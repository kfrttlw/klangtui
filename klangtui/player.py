"""Audio through mpv, driven over its JSON IPC socket.

mpv runs once per session in idle mode with no window and no terminal. We
send it commands (load, pause, seek, volume) and it pushes events back —
position, pause state, end of file — so nothing has to be polled.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


class PlayerError(Exception):
    """mpv is missing, died, or refused a command."""


def find_mpv() -> str | None:
    return os.environ.get("KLANGTUI_MPV") or shutil.which("mpv")


MPV_HINT = ("mpv isn't installed — klangtui plays audio through it: "
            "sudo pacman -S mpv · sudo apt install mpv · brew install mpv")


class Mpv:
    """One mpv process. Events arrive on a reader thread as
    on_event(name, value): time, pause, duration, buffering, ended, error, closed.
    """

    # observed properties (id → name); time-pos is throttled before it's passed on
    _OBSERVE = {1: "time-pos", 2: "pause", 3: "duration", 4: "paused-for-cache"}

    def __init__(self, on_event):
        self._on_event = on_event
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._dir: str | None = None
        self._wlock = threading.Lock()
        self._slock = threading.Lock()          # load bookkeeping (UI vs reader thread)
        self._req = 0
        self._waiting: dict[int, list] = {}      # request_id → [Event, reply]
        self._pending_loads = 0                  # loadfiles sent, no start-file yet
        self._entry: int | None = None           # playlist_entry_id now playing
        self._seek_on_load: float | None = None
        self._last_t = -1.0

    # --- lifecycle ---

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._sock is not None

    def start(self, volume: int):
        exe = find_mpv()
        if not exe:
            raise PlayerError(MPV_HINT)
        if sys.platform == "win32":
            raise PlayerError("the mpv backend needs a Unix socket (Linux / macOS)")
        # a private directory for the socket: anyone who can reach an mpv IPC
        # socket can make mpv run programs, so never leave it world-writable
        base = os.environ.get("XDG_RUNTIME_DIR")
        self._dir = tempfile.mkdtemp(prefix="klangtui-", dir=base if base and
                                     os.path.isdir(base) else None)
        path = str(Path(self._dir) / "mpv.sock")
        args = [
            exe, "--idle=yes", "--no-video", "--no-terminal", "--force-window=no",
            f"--input-ipc-server={path}", f"--volume={volume}", "--volume-max=100",
            "--cache=yes", "--ytdl=no", "--keep-open=no", "--audio-display=no",
            "--no-resume-playback", "--save-position-on-quit=no",
            "--audio-client-name=klangtui", "--title=klangtui",
        ]
        try:
            self._proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE)
        except OSError as e:
            raise PlayerError(f"couldn't start mpv: {e}") from e
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + 5
        while True:
            if self._proc.poll() is not None:
                err = (self._proc.stderr.read() or b"").decode(errors="replace").strip()
                raise PlayerError(f"mpv quit right away: {err.splitlines()[-1] if err else '?'}")
            try:
                sock.connect(path)
                break
            except OSError:
                if time.monotonic() > deadline:
                    self._proc.kill()
                    raise PlayerError("mpv didn't open its control socket") from None
                time.sleep(0.05)
        self._sock = sock
        threading.Thread(target=self._read_loop, name="klangtui-mpv", daemon=True).start()
        for pid, name in self._OBSERVE.items():
            self._send(["observe_property", pid, name])

    def close(self):
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                self._send(["quit"])
                proc.wait(timeout=1.5)
            except (PlayerError, subprocess.TimeoutExpired):
                proc.kill()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    # --- commands ---

    def _send(self, cmd: list, wait: bool = False):
        if self._sock is None:
            raise PlayerError("mpv isn't running")
        with self._wlock:
            self._req += 1
            rid = self._req
            slot = [threading.Event(), None]
            if wait:
                self._waiting[rid] = slot
            line = json.dumps({"command": cmd, "request_id": rid}) + "\n"
            try:
                self._sock.sendall(line.encode())
            except OSError as e:
                self._waiting.pop(rid, None)
                raise PlayerError("lost the connection to mpv") from e
        if not wait:
            return None
        if not slot[0].wait(3):
            self._waiting.pop(rid, None)
            raise PlayerError(f"mpv didn't answer {cmd[0]}")
        reply = slot[1] or {}
        if reply.get("error") not in (None, "success"):
            raise PlayerError(f"mpv: {reply['error']}")
        return reply.get("data")

    def load(self, url: str, start: float = 0.0):
        """Replace whatever plays with url (optionally starting at `start` s)."""
        with self._slock:
            self._seek_on_load = start if start > 1 else None
            self._pending_loads += 1
            self._last_t = -1.0
        try:
            self._send(["loadfile", url, "replace"], wait=True)
        except PlayerError:
            with self._slock:
                self._pending_loads = max(0, self._pending_loads - 1)
            raise
        self._send(["set_property", "pause", False])

    def set_pause(self, paused: bool):
        self._send(["set_property", "pause", paused])

    def seek(self, seconds: float, relative: bool):
        self._send(["seek", seconds, "relative" if relative else "absolute"])

    def set_volume(self, volume: int):
        self._send(["set_property", "volume", volume])

    def set_loop(self, on: bool):
        self._send(["set_property", "loop-file", "inf" if on else "no"])

    def stop(self):
        self._send(["stop"])

    # --- events ---

    def _emit(self, name: str, value=None):
        try:
            self._on_event(name, value)
        except Exception:
            pass

    def _read_loop(self):
        buf = b""
        sock = self._sock
        while True:
            try:
                chunk = sock.recv(65536) if sock else b""
            except OSError:
                chunk = b""
            if not chunk:
                if self._proc is not None:          # not a close() we asked for
                    self._emit("closed")
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                self._handle(msg)

    def _handle(self, msg: dict):
        if "request_id" in msg and "event" not in msg:
            slot = self._waiting.pop(msg["request_id"], None)
            if slot:
                slot[1] = msg
                slot[0].set()
            return
        ev = msg.get("event")
        if ev == "property-change":
            name, data = msg.get("name"), msg.get("data")
            if name == "time-pos":
                if data is None:
                    return
                # mpv reports many times a second — the UI needs ~2 updates/s
                if abs(data - self._last_t) >= 0.5:
                    self._last_t = data
                    self._emit("time", data)
            elif name == "pause":
                self._emit("pause", bool(data))
            elif name == "duration":
                if data:
                    self._emit("duration", data)
            elif name == "paused-for-cache":
                self._emit("buffering", bool(data))
        elif ev == "start-file":
            with self._slock:
                self._pending_loads = max(0, self._pending_loads - 1)
                self._entry = msg.get("playlist_entry_id")
        elif ev == "file-loaded":
            with self._slock:
                pos, self._seek_on_load = self._seek_on_load, None
            if pos:
                try:
                    self.seek(pos, relative=False)
                except PlayerError:
                    pass
        elif ev == "seek":
            self._last_t = -1.0                     # report the new spot at once
        elif ev == "end-file":
            # a file we replaced ends with reason "stop" — only a real end of
            # the current file, with no newer load on its way, counts
            with self._slock:
                stale = (self._pending_loads > 0
                         or msg.get("playlist_entry_id") not in (None, self._entry))
            if stale:
                return
            reason = msg.get("reason")
            if reason == "eof":
                self._emit("ended")
            elif reason == "error":
                self._emit("error", msg.get("file_error") or "playback failed")
