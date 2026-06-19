#!/usr/bin/env python3
"""klangtui — SoundCloud player in your terminal: search, play, like, playlists."""

import argparse
import atexit
import base64
import queue
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich import box

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import (
    Horizontal, HorizontalScroll, Vertical, VerticalScroll
)
from textual.theme import Theme
from textual.widgets import Input, Static

# -----------------------------------------------------------------------------
#  Local data (~/.klangtui) — settings DB + the persistent Firefox profile
# -----------------------------------------------------------------------------

DATA_DIR    = Path.home() / ".klangtui"
DB_PATH     = DATA_DIR / "db.sqlite"
PROFILE_DIR = DATA_DIR / "profile"        # Firefox profile → the SoundCloud login
CID_PATH    = DATA_DIR / "client_id"      # cached api client_id (changes rarely)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DB:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(DB_PATH)
        self.con.executescript(_SCHEMA)
        self.con.commit()

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key: str, value: str):
        self.con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
        self.con.commit()


# -----------------------------------------------------------------------------
#  SoundCloud — we keep a real soundcloud.com session in a headless Firefox
#
#  The browser does three jobs: (1) it holds the login — /login opens a normal
#  Firefox window, you sign in, the cookies land in a persistent profile;
#  (2) its cookie jar authenticates our calls to SoundCloud's own api-v2 (the
#  same API the website uses), via Playwright's request context; (3) it *plays
#  the audio* — an <audio> element in a hidden page, so there is no mpv/ffmpeg
#  to install and the same code works on Linux, macOS and Windows.
# -----------------------------------------------------------------------------

_API = "https://api-v2.soundcloud.com"


class ApiError(Exception):
    """SoundCloud answered with something we can't use."""


class AuthError(Exception):
    """The action needs a signed-in account."""


class PlaybackError(Exception):
    """The track can't be played (no progressive stream, autoplay refused…)."""


# --- Playwright browser (one persistent context, reused for the whole session) ---

_pw_instance = None
_pw_ctx      = None   # persistent BrowserContext — cookie jar lives in PROFILE_DIR
_player_page = None   # hidden about:blank page that owns the <audio> element
_sc_page     = None   # page parked on soundcloud.com, for authenticated writes
_client_id   = None   # api-v2 client_id, sniffed from the real site
_me: dict | None = None     # /me payload when signed in
_me_checked  = False


_firefox_install_tried = False


def _ensure_firefox_installed(on_status=None) -> bool:
    """Download Playwright's Firefox build if it's missing.

    Runs at most once per process — so if the download itself fails (no network,
    say) it won't loop and keep re-fetching Firefox on every retry.
    """
    global _firefox_install_tried
    if _firefox_install_tried:
        return False
    _firefox_install_tried = True
    if on_status:
        on_status("first run: downloading Firefox (~80 MB)…")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "firefox"],
            check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


# Firefox prefs: allow autoplay (our audio.play() runs with no user gesture),
# don't pause media in background tabs, and turn off the automation/webdriver
# flag so SoundCloud's bot-check doesn't trip on a plain personal login.
_FIREFOX_PREFS = {
    "media.autoplay.default": 0,
    "media.autoplay.blocking_policy": 0,
    "media.block-autoplay-until-in-foreground": False,
    "dom.webdriver.enabled": False,
    "useAutomationExtension": False,
    # Trust the OS root store (like Chrome/Edge) so antivirus HTTPS-scanning,
    # which MITMs TLS with its own root cert, doesn't break sndcdn.com et al.
    "security.enterprise_roots.enabled": True,
}

# Runs before any page script: a normal browser reports navigator.webdriver
# === false, so make ours look the same (Playwright otherwise leaves it true).
_STEALTH_JS = (
    "try { Object.defineProperty(navigator, 'webdriver', "
    "{get: () => false, configurable: true}); } catch (e) {}"
)


def _new_persistent_ctx(kwargs):
    ctx = _pw_instance.firefox.launch_persistent_context(str(PROFILE_DIR), **kwargs)
    try:
        ctx.add_init_script(_STEALTH_JS)
    except Exception:
        pass
    return ctx


def _launch_ctx(headless: bool, on_status=None):
    """Launch the persistent Firefox context (downloads Firefox on first run)."""
    global _pw_instance
    if _pw_instance is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ConnectionError(
                "playwright is not installed.\n  run: pip install playwright"
            )
        _pw_instance = sync_playwright().start()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # Don't spoof a User-Agent: let this Firefox report its own real, current UA.
    # A hard-coded version string goes stale and SoundCloud's sign-in then refuses
    # it as an "outdated browser".
    kwargs = dict(
        headless=headless,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        firefox_user_prefs=_FIREFOX_PREFS,
    )
    try:
        return _new_persistent_ctx(kwargs)
    except Exception as e:
        # 99% of the time this just means firefox isn't downloaded yet —
        # grab it once and retry, otherwise let the real error through
        need = "Executable doesn't exist" in str(e) or "playwright install" in str(e)
        if need and _ensure_firefox_installed(on_status):
            try:
                return _new_persistent_ctx(kwargs)
            except Exception as e2:
                raise ConnectionError(f"Firefox failed to launch after install: {e2}") from e2
        raise ConnectionError(
            f"Firefox failed to launch: {e}\n  run: playwright install firefox"
        ) from e


def _ensure_browser(on_status=None):
    global _pw_ctx
    if _pw_ctx is None:
        if on_status:
            on_status("starting browser…")
        _pw_ctx = _launch_ctx(headless=True, on_status=on_status)
    return _pw_ctx


def _close_browser():
    """Close the context (flushes cookies to the profile) and the open pages."""
    global _pw_ctx, _player_page, _sc_page
    for pg in (_player_page, _sc_page):
        if pg:
            try:
                pg.close()
            except Exception:
                pass
    _player_page = None
    _sc_page = None
    if _pw_ctx:
        try:
            _pw_ctx.close()
        except Exception:
            pass
        _pw_ctx = None


def _stop_playwright():
    global _pw_instance
    _close_browser()
    if _pw_instance:
        try:
            _pw_instance.stop()
        except Exception:
            pass
        _pw_instance = None


atexit.register(_stop_playwright)


# --- auth: the login is just the oauth_token cookie of the real session ---

def _cookie_token(ctx=None) -> str | None:
    ctx = ctx or _pw_ctx
    if ctx is None:
        return None
    try:
        for c in ctx.cookies("https://soundcloud.com"):
            if c.get("name") == "oauth_token" and c.get("value"):
                return c["value"]
    except Exception:
        return None
    return None


def _auth_headers() -> dict:
    tok = _cookie_token()
    return {"Authorization": f"OAuth {tok}"} if tok else {}


# --- api-v2 plumbing (Playwright's request context shares the cookie jar) ---

def _api_url(path: str, **params) -> str:
    params.setdefault("client_id", _client_id)
    return f"{_API}{path}?{urlencode(params)}"


def _api_get(path: str, **params) -> dict | list:
    r = _pw_ctx.request.get(_api_url(path, **params), headers=_auth_headers())
    if r.status == 401:
        raise AuthError("SoundCloud session missing or expired — /login")
    if r.status == 403:
        raise ApiError("SoundCloud refused the request (403) — try again in a minute")
    if r.status == 404:
        raise ApiError("not found (404)")
    if r.status == 429:
        raise ApiError("SoundCloud rate limit — wait a little and try again")
    if not r.ok:
        raise ApiError(f"SoundCloud error (HTTP {r.status})")
    return r.json()


def _client_id_works(cid: str) -> bool:
    try:
        r = _pw_ctx.request.get(
            f"{_API}/search/tracks?q=a&limit=1&client_id={cid}"
        )
        return r.ok
    except Exception:
        return False


def _discover_client_id(on_status=None) -> str:
    """Sniff the api client_id the real site uses.

    Open soundcloud.com once and watch its own api-v2 requests go by — the
    client_id rides in every query string. Fall back to regexing the JS
    bundles if the sniff comes up empty.
    """
    if on_status:
        on_status("connecting to SoundCloud…")
    found: dict = {}

    def sniff(req):
        if "client_id=" in req.url and "api-v2.soundcloud.com" in req.url:
            m = re.search(r"[?&]client_id=([A-Za-z0-9]{16,})", req.url)
            if m:
                found["id"] = m.group(1)

    pg = _pw_ctx.new_page()
    try:
        pg.on("request", sniff)
        pg.goto("https://soundcloud.com/discover",
                wait_until="domcontentloaded", timeout=45_000)
        for _ in range(100):                      # up to ~15s
            if "id" in found:
                break
            pg.wait_for_timeout(150)
        cid = found.get("id")
        if not cid:
            srcs = pg.eval_on_selector_all(
                "script[src]", "els => els.map(e => e.src)"
            )
            for src in reversed([s for s in srcs if "sndcdn.com" in s]):
                try:
                    r = _pw_ctx.request.get(src)
                    if not r.ok:
                        continue
                    m = re.search(r'client_id\s*[:=]\s*"([A-Za-z0-9]{16,})"', r.text())
                    if m:
                        cid = m.group(1)
                        break
                except Exception:
                    continue
    finally:
        try:
            pg.close()
        except Exception:
            pass
    if not cid:
        raise ApiError("couldn't obtain a SoundCloud client_id — try again later")
    return cid


def _ensure_client_id(on_status=None):
    """Load the cached client_id (validating it), or sniff a fresh one."""
    global _client_id
    if _client_id:
        return
    cached = None
    try:
        cached = CID_PATH.read_text().strip() or None
    except OSError:
        pass
    if cached and _client_id_works(cached):
        _client_id = cached
        return
    _client_id = _discover_client_id(on_status)
    try:
        CID_PATH.write_text(_client_id)
    except OSError:
        pass


def _fetch_me() -> dict | None:
    global _me
    _me = None
    if _cookie_token():
        try:
            me = _api_get("/me")
            if isinstance(me, dict) and me.get("id"):
                _me = me
        except (ApiError, AuthError):
            pass
    return _me


def _ensure_ready(on_status=None):
    """Browser up, client_id known, login state probed (once)."""
    global _me_checked
    _ensure_browser(on_status)
    _ensure_client_id(on_status)
    if not _me_checked:
        _me_checked = True
        _fetch_me()


def _require_me() -> dict:
    if not _me:
        raise AuthError("you're not signed in — /login first")
    return _me


# --- playback: a hidden <audio> element in the headless browser ---
#
# Headless Firefox still routes media through the normal audio backend, so
# audio.play() really comes out of the speakers — no external player needed.

_JS_PLAY = """
async (args) => {
  let a = document.getElementById('klang');
  if (!a) {
    a = document.createElement('audio');
    a.id = 'klang';
    a.preload = 'auto';
    document.body.appendChild(a);
  }
  a.src = args.url;
  a.volume = args.vol;
  try { await a.play(); return 'ok'; } catch (e) { return String(e); }
}
"""

_JS_STATE = """
() => {
  const a = document.getElementById('klang');
  if (!a || !a.src) return null;
  return {t: a.currentTime, d: a.duration || 0, p: a.paused, e: a.ended};
}
"""

_JS_TOGGLE = """
async () => {
  const a = document.getElementById('klang');
  if (!a || !a.src) return 'no';
  if (a.paused) { try { await a.play(); } catch (e) { return String(e); } return 'playing'; }
  a.pause(); return 'paused';
}
"""

_JS_PAUSE  = """
() => { const a = document.getElementById('klang'); if (a && a.src) a.pause(); }
"""

_JS_RESUME = """
async () => {
  const a = document.getElementById('klang');
  if (!a || !a.src) return 'no';
  try { await a.play(); return 'ok'; } catch (e) { return String(e); }
}
"""

_JS_SEEK = """
(args) => {
  const a = document.getElementById('klang');
  if (!a || !a.src) return 'no';
  let t = args.rel ? a.currentTime + args.value : args.value;
  if (isFinite(a.duration) && a.duration > 0) t = Math.min(t, a.duration - 0.4);
  a.currentTime = Math.max(0, t);
  return 'ok';
}
"""

_JS_VOLUME = """
(v) => { const a = document.getElementById('klang'); if (a) a.volume = v; }
"""

# The browser doubles as our image decoder: artwork bytes go in as a data URL,
# a canvas scales them down, and the raw RGBA pixels come back for the terminal
# to paint with half-blocks. No Pillow, no native deps.
_JS_ART = """
async (args) => {
  const img = new Image();
  await new Promise((res, rej) => {
    img.onload = res; img.onerror = () => rej(new Error('decode'));
    img.src = args.data;
  });
  const c = document.createElement('canvas');
  c.width = args.w; c.height = args.h;
  const g = c.getContext('2d');
  g.imageSmoothingEnabled = true;
  g.imageSmoothingQuality = 'high';
  g.drawImage(img, 0, 0, args.w, args.h);
  return Array.from(g.getImageData(0, 0, args.w, args.h).data);
}
"""

_art_pixel_cache: dict = {}   # (url, px) -> flat RGBA list | None


def _art_sources(url: str, px: int) -> list[str]:
    """URLs to try for one image, sharpest first.

    SoundCloud serves `-large.jpg` at a tiny 100×100; `-t500x500.jpg` is the
    same artwork at 500px, so downsampling it looks far cleaner.  We only reach
    for it when the tile is big enough to show the detail, and always keep the
    original as a fallback in case the bigger variant 404s."""
    if px >= 10:
        hi = re.sub(r"-large(\.[a-zA-Z]+)$", r"-t500x500\1", url)
        if hi != url:
            return [hi, url]
    return [url]


def _ensure_player():
    global _player_page
    if _player_page is None or _player_page.is_closed():
        _player_page = _pw_ctx.new_page()
    return _player_page


# A like/unlike is a write, and SoundCloud guards writes with a bot-check
# (DataDome). Issuing it from inside a real soundcloud.com page makes it carry
# the browser's cookies, Origin and DataDome token — exactly what the site sends.
_JS_WRITE = """
async (args) => {
  try {
    const r = await fetch(args.url, {
      method: args.method,
      headers: { 'Authorization': 'OAuth ' + args.token, 'Accept': 'application/json' },
      credentials: 'include',
    });
    return r.status;
  } catch (e) { return -1; }
}
"""


def _ensure_sc_page():
    """A page parked on soundcloud.com, reused for authenticated writes."""
    global _sc_page
    if _sc_page is None or _sc_page.is_closed():
        _sc_page = _pw_ctx.new_page()
        _sc_page.goto("https://soundcloud.com/",
                      wait_until="domcontentloaded", timeout=45_000)
    return _sc_page


def _resolve_stream(track: dict) -> tuple[str, bool, dict]:
    """Track → a playable URL. Returns (signed_url, is_preview, full_track).

    SoundCloud serves most public tracks as a plain progressive mp3 next to the
    HLS variants — that's the one an <audio> element can play directly. Tracks
    that only ship HLS (rare; usually Go+ catalogue) are reported, not played:
    klangtui never tries to get around what the web player would allow.
    """
    transcodings = (track.get("media") or {}).get("transcodings") or []
    if not transcodings:
        track = _api_get(f"/tracks/{track['id']}")
        transcodings = (track.get("media") or {}).get("transcodings") or []
    prog = [t for t in transcodings
            if (t.get("format") or {}).get("protocol") == "progressive"]
    if not prog:
        raise PlaybackError(
            "this track has no progressive stream (HLS/Go+ only) — can't play it"
        )
    chosen = prog[0]
    sep = "&" if "?" in chosen["url"] else "?"
    url = f"{chosen['url']}{sep}client_id={_client_id}"
    if track.get("track_authorization"):
        url += f"&track_authorization={track['track_authorization']}"
    r = _pw_ctx.request.get(url, headers=_auth_headers())
    if not r.ok:
        raise PlaybackError(f"couldn't resolve the stream (HTTP {r.status})")
    signed = (r.json() or {}).get("url")
    if not signed:
        raise PlaybackError("SoundCloud returned no stream URL")
    return signed, bool(chosen.get("snipped")), track


# --- data helpers -------------------------------------------------------------

def _collection(j) -> list:
    return j.get("collection", []) if isinstance(j, dict) else (j or [])


def _item_kind(item: dict) -> str:
    """track / playlist / user — playlists include albums and system stations."""
    k = item.get("kind") or ""
    if k == "track":
        return "track"
    if k == "user":
        return "user"
    if "playlist" in k or item.get("tracks") is not None:
        return "playlist"
    return "?"


def _hydrate_tracks(track_stubs: list[dict]) -> list[dict]:
    """A playlist body mixes full track objects with bare {id} stubs — fetch
    the stubs in batches and put everything back in playlist order."""
    full  = {t["id"]: t for t in track_stubs if t.get("title")}
    stubs = [t["id"] for t in track_stubs if not t.get("title")]
    for i in range(0, len(stubs), 20):
        chunk = stubs[i:i + 20]
        try:
            got = _api_get("/tracks", ids=",".join(str(x) for x in chunk))
        except ApiError:
            continue
        for t in got:
            full[t["id"]] = t
    return [full[t["id"]] for t in track_stubs if t["id"] in full]


_PLAYLIST_TRACK_CAP = 100   # don't hydrate 500-track playlists in one go


def _playlist_tracks(pl_json: dict) -> tuple[list[dict], int]:
    """(tracks, how_many_more_were_not_loaded) for a fetched playlist body."""
    stubs  = (pl_json.get("tracks") or [])[:_PLAYLIST_TRACK_CAP]
    tracks = _hydrate_tracks(stubs)
    total  = pl_json.get("track_count") or len(pl_json.get("tracks") or [])
    return tracks, max(0, total - len(stubs))


def _fetch_playlist_body(pl: dict) -> dict:
    """A playlist's full body, trying every endpoint that might serve it.

    Discover and the library mix kinds: ordinary playlists live at
    /playlists/{id}, SoundCloud's own stations/charts at
    /system_playlists/{urn}, and a few only answer to their permalink.  We try
    the most likely first and fall back, so opening one never dead-ends on a 404
    just because it was tagged differently than we guessed."""
    pid = pl.get("id")
    urn = pl.get("urn")
    url = pl.get("permalink_url")
    is_system = bool(pl.get("_system") or "system" in (pl.get("kind") or ""))
    attempts = ([("/system_playlists/%s", urn), ("/playlists/%s", pid)]
                if is_system else
                [("/playlists/%s", pid), ("/system_playlists/%s", urn)])
    last: Exception | None = None
    for tmpl, key in attempts:
        if not key:
            continue
        try:
            return _api_get(tmpl % key)
        except ApiError as e:
            last = e
    if url:                                  # last resort: resolve the permalink
        try:
            return _api_get("/resolve", url=url)
        except ApiError as e:
            last = e
    raise last or ApiError("couldn't open this playlist")


# -----------------------------------------------------------------------------
#  Backend — one dedicated thread owns every blocking / Playwright call
# -----------------------------------------------------------------------------

class SCBackend:
    """One worker thread owns the whole SoundCloud browser session.

    Playwright's sync API won't run inside Textual's asyncio loop, so the UI
    never touches it directly — it drops a job on the queue and this thread
    drives the browser. Results come back through callbacks the app wraps with
    `call_from_thread`. Between jobs the thread polls the <audio> element and
    streams play-position ticks back to the UI (and fires on_ended for the
    queue's auto-advance).
    """

    def __init__(self, on_tick, on_ended):
        self._jobs: "queue.Queue" = queue.Queue()
        self._on_tick  = on_tick
        self._on_ended = on_ended
        self._active = False        # something has been loaded into the player
        self._ended_fired = False
        self._thread = threading.Thread(
            target=self._loop, name="klangtui-backend", daemon=True
        )
        self._thread.start()

    def submit(self, kind: str, *, on_status=None, on_done=None, on_error=None, **kw):
        self._jobs.put(dict(kind=kind, on_status=on_status, on_done=on_done,
                            on_error=on_error, **kw))

    def shutdown(self):
        self._jobs.put(None)

    # --- worker thread ---

    def _loop(self):
        while True:
            try:
                job = self._jobs.get(timeout=0.6)
            except queue.Empty:
                self._tick()
                continue
            if job is None:
                _close_browser()
                return
            try:
                result = self._dispatch(job)
            except Exception as e:  # a dead worker thread would freeze the UI
                cb = job.get("on_error")
                if cb:
                    try:
                        cb(self._friendly(e))
                    except Exception:
                        pass
                continue
            cb = job.get("on_done")
            if cb:
                try:
                    cb(result)
                except Exception:
                    pass

    @staticmethod
    def _friendly(e: Exception) -> str:
        if isinstance(e, (ApiError, AuthError, PlaybackError, ConnectionError)):
            return str(e)
        return f"internal error: {e}"

    def _tick(self):
        """Idle heartbeat: report the play position, notice the track ending."""
        if not self._active or _player_page is None:
            return
        try:
            st = _player_page.evaluate(_JS_STATE)
        except Exception:
            return
        if not st:
            return
        try:
            self._on_tick(st)
        except Exception:
            pass
        if st.get("e") and not self._ended_fired:
            self._ended_fired = True
            try:
                self._on_ended()
            except Exception:
                pass

    def _dispatch(self, job: dict):
        global _me, _me_checked
        kind = job["kind"]
        on_status = job.get("on_status") or (lambda s: None)
        if kind == "connect":
            _ensure_ready(on_status)
            return {"user": _me}
        if kind == "search":
            _ensure_ready(on_status)
            on_status("searching…")
            # "all" is the site's own mixed search — tracks, playlists and
            # people interleaved, just like the web search page
            path = {"all":       "/search",
                    "tracks":    "/search/tracks",
                    "playlists": "/search/playlists",
                    "people":    "/search/users"}[job.get("scope", "all")]
            j = _api_get(path, q=job["q"], limit=20)
            return [it for it in _collection(j) if _item_kind(it) != "?"]
        if kind == "discover":
            _ensure_ready(on_status)
            on_status("loading discover…")
            j = _api_get("/mixed-selections", limit=12)
            sections = []
            for sel in _collection(j):
                pls = []
                for it in _collection(sel.get("items") or {})[:8]:
                    if _item_kind(it) == "playlist":
                        pls.append(it)
                if pls:
                    sections.append({"title": sel.get("title") or "selection",
                                     "playlists": pls})
            if not sections:
                raise ApiError("discover came back empty — try again later")
            return sections
        if kind == "user_tracks":
            _ensure_ready(on_status)
            on_status("loading tracks…")
            j = _api_get(f"/users/{job['user']['id']}/tracks", limit=50)
            return _collection(j)
        if kind == "art":
            return self._handle_art(job)
        if kind == "open_url":
            _ensure_ready(on_status)
            return self._handle_open(job["url"], on_status)
        if kind == "related":
            _ensure_ready(on_status)
            on_status("tuning the radio…")
            j = _api_get(f"/tracks/{job['track']['id']}/related", limit=25)
            return _collection(j)
        if kind == "play":
            return self._handle_play(job, on_status)
        if kind == "pause":
            self._player_eval(_JS_PAUSE)
            return None
        if kind == "resume":
            res = self._player_eval(_JS_RESUME)
            if res == "no":
                raise PlaybackError("nothing is loaded — /play <n> first")
            if res != "ok":
                raise PlaybackError(f"browser refused to resume: {res}")
            return None
        if kind == "toggle":
            res = self._player_eval(_JS_TOGGLE)
            if res == "no":
                raise PlaybackError("nothing is playing")
            if res not in ("playing", "paused"):
                raise PlaybackError(f"browser refused: {res}")
            return res
        if kind == "seek":
            res = self._player_eval(_JS_SEEK, {"rel": job["rel"], "value": job["value"]})
            if res == "no":
                raise PlaybackError("nothing is playing")
            return None
        if kind == "volume":
            if _player_page is not None and not _player_page.is_closed():
                self._player_eval(_JS_VOLUME, job["value"] / 100)
            return None
        if kind == "like":
            _ensure_ready(on_status)
            me = _require_me()
            self._like_request("put", me["id"], job["track"]["id"])
            return None
        if kind == "unlike":
            _ensure_ready(on_status)
            me = _require_me()
            self._like_request("delete", me["id"], job["track"]["id"])
            return None
        if kind == "likes":
            _ensure_ready(on_status)
            me = _require_me()
            on_status("fetching your likes…")
            j = _api_get(f"/users/{me['id']}/track_likes", limit=50)
            return [e["track"] for e in _collection(j) if e.get("track")]
        if kind == "playlists":
            _ensure_ready(on_status)
            _require_me()
            on_status("fetching your library…")
            j = _api_get("/me/library/all", limit=50)
            out = []
            for it in _collection(j):
                pl = it.get("playlist") or it.get("system_playlist")
                if pl:
                    pl["_system"] = "system_playlist" in it
                    out.append(pl)
            return out
        if kind == "playlist_tracks":
            _ensure_ready(on_status)
            pl = job["playlist"]
            on_status("loading playlist…")
            body = _fetch_playlist_body(pl)
            tracks, more = _playlist_tracks(body)
            return {"title": pl.get("title") or "playlist", "tracks": tracks, "more": more}
        if kind == "profile":
            _ensure_ready(on_status)
            _require_me()
            _me = _api_get("/me")
            return _me
        if kind == "login":
            return self._handle_login(on_status)
        if kind == "logout":
            _ensure_ready(on_status)
            _pw_ctx.clear_cookies()
            _me = None
            _me_checked = True
            return None
        raise RuntimeError(f"unknown job: {kind}")

    def _player_eval(self, js: str, arg=None):
        if _player_page is None or _player_page.is_closed():
            raise PlaybackError("nothing is playing")
        return _player_page.evaluate(js) if arg is None else _player_page.evaluate(js, arg)

    def _like_request(self, method: str, user_id, track_id):
        url = _api_url(f"/users/{user_id}/track_likes/{track_id}")
        tok = _cookie_token()
        if not tok:
            raise AuthError("you're not signed in — /login first")
        # 1) the lightweight request context, now with the browser's own
        #    Origin/Referer so the write doesn't look like a bare script
        fn = _pw_ctx.request.put if method == "put" else _pw_ctx.request.delete
        r = fn(url, headers={
            **_auth_headers(),
            "Origin": "https://soundcloud.com",
            "Referer": "https://soundcloud.com/",
            "Accept": "application/json",
        })
        if r.status in (200, 201):
            return
        if r.status == 401:
            raise AuthError("SoundCloud session expired — /login again")
        if r.status != 403:
            raise ApiError(f"SoundCloud refused (HTTP {r.status}) — try again later")
        # 2) 403 = write/bot protection. Retry from inside a real soundcloud.com
        #    page, so it carries the cookies, Origin and DataDome token the site
        #    uses for its own likes.
        status = _ensure_sc_page().evaluate(
            _JS_WRITE, {"url": url, "method": method.upper(), "token": tok})
        if status in (200, 201):
            return
        if status == 401:
            raise AuthError("SoundCloud session expired — /login again")
        if status == 403:
            raise ApiError("SoundCloud is bot-checking writes (403) — open /login "
                           "once, pass the 'human check' in the window, then try again")
        raise ApiError(f"SoundCloud refused the like (HTTP {status})")

    def _handle_art(self, job: dict):
        url, px = job["url"], job["px"]
        quad = job.get("quad", False)
        # half-block art packs 1x2 px per cell; quadrant packs 2x2 → twice the
        # columns of detail at the same on-screen size.  Height is the same.
        w = px * 2 if quad else px
        h = px
        key = (url, w, h)
        if key not in _art_pixel_cache:
            pixels = None
            for src in _art_sources(url, px):
                try:
                    _ensure_browser()
                    r = _pw_ctx.request.get(src)
                    if not r.ok:
                        continue
                    data = base64.b64encode(r.body()).decode()
                    pg = _ensure_player()
                    raw = pg.evaluate(_JS_ART, {
                        "data": f"data:image/jpeg;base64,{data}", "w": w, "h": h,
                    })
                    if raw and len(raw) >= w * h * 4:
                        pixels = raw
                        break
                except Exception:
                    continue
            _art_pixel_cache[key] = pixels
        return {"url": url, "px": px, "quad": quad, "pixels": _art_pixel_cache[key]}

    def _handle_play(self, job: dict, on_status):
        _ensure_ready(on_status)
        on_status("resolving stream…")
        url, snipped, track = _resolve_stream(job["track"])
        wave = _fetch_wave(track, job["bar_width"])
        on_status("starting playback…")
        pg  = _ensure_player()
        res = pg.evaluate(_JS_PLAY, {"url": url, "vol": job["volume"] / 100})
        if res != "ok":
            raise PlaybackError(f"browser refused to play: {res}")
        self._active = True
        self._ended_fired = False
        return {"track": track, "snipped": snipped, "wave": wave}

    def _handle_open(self, url: str, on_status):
        on_status("resolving link…")
        j = _api_get("/resolve", url=url)
        kind = j.get("kind")
        if kind == "track":
            return {"kind": "track", "track": j}
        if kind == "playlist" or "tracks" in j:
            tracks, more = _playlist_tracks(j)
            return {"kind": "playlist", "title": j.get("title") or "playlist",
                    "tracks": tracks, "more": more}
        if kind == "user":
            t = _api_get(f"/users/{j['id']}/tracks", limit=50)
            return {"kind": "user",
                    "title": f"tracks by {j.get('username') or 'user'}",
                    "tracks": _collection(t), "more": 0}
        raise ApiError(f"can't open that link (kind: {kind or 'unknown'})")

    def _handle_login(self, on_status):
        """Open a *visible* Firefox window on the same profile and wait for the
        user to sign in there. Real page, real captcha handling — we only watch
        for the oauth_token cookie to appear, then go back to headless."""
        global _me_checked
        on_status("opening a Firefox window — sign in to SoundCloud there…")
        self._active = False            # the player dies with the headless browser
        _close_browser()
        ctx = _launch_ctx(headless=False, on_status=on_status)
        token = None
        try:
            pg = ctx.new_page()
            # Land on the homepage and let the user click "Sign in" — the site's
            # own flow. A cold jump straight to /signin tends to throw SoundCloud's
            # "something went wrong" page.
            pg.goto("https://soundcloud.com/",
                    wait_until="domcontentloaded", timeout=60_000)
            on_status("in the window: pass any 'human check', click “Sign in”, "
                      "log in… (closing the window cancels)")
            for _ in range(600):        # up to ~10 minutes
                try:
                    if not ctx.pages:   # user closed the window
                        break
                    token = _cookie_token(ctx)
                    if token:
                        break
                except Exception:
                    break
                time.sleep(1)
        finally:
            try:
                ctx.close()             # flushes the cookies into the profile
            except Exception:
                pass
        _me_checked = False
        if not token:
            raise AuthError("the window closed before you signed in — nothing changed")
        on_status("signed in — restarting headless…")
        _ensure_ready(on_status)
        if not _me:
            raise AuthError("signed in, but SoundCloud didn't accept the session — "
                            "try /login again")
        return _me


# -----------------------------------------------------------------------------
#  Formatting helpers
# -----------------------------------------------------------------------------

def _fmt_time(seconds: float | None) -> str:
    if seconds is None or seconds != seconds or seconds < 0:   # NaN-safe
        return "?:??"
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _fmt_count(n) -> str:
    if not isinstance(n, (int, float)):
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def _track_artist(track: dict) -> str:
    return (track.get("user") or {}).get("username") or "?"


def _track_secs(track: dict) -> float:
    return (track.get("duration") or 0) / 1000


# --- the waveform progress bar — SoundCloud's signature scrubber, in glyphs ---

_WAVE_GLYPHS = "▁▂▃▄▅▆▇█"


def _downsample_wave(samples: list, height: int, width: int) -> list[int] | None:
    """SoundCloud's waveform JSON → `width` glyph levels (0–7)."""
    if not samples or height <= 0:
        return None
    n = len(samples)
    out = []
    for i in range(width):
        lo = i * n // width
        hi = max(lo + 1, (i + 1) * n // width)
        # average, not peak — peaks saturate on loud masters and flatten the bar
        bucket = samples[lo:hi]
        avg = sum(bucket) / len(bucket)
        out.append(max(0, min(7, int(8 * avg / height))))
    return out


def _art_text(pixels: list, px: int) -> Text:
    """Flat RGBA pixels → a px-wide, px/2-tall block of ▀ half-block 'pixels'."""
    t = Text()
    for row in range(0, px, 2):
        if row:
            t.append("\n")
        for col in range(px):
            i1 = (row * px + col) * 4
            i2 = ((row + 1) * px + col) * 4
            top = f"#{pixels[i1]:02x}{pixels[i1 + 1]:02x}{pixels[i1 + 2]:02x}"
            bot = f"#{pixels[i2]:02x}{pixels[i2 + 1]:02x}{pixels[i2 + 2]:02x}"
            t.append("▀", style=f"{top} on {bot}")
    return t


# Each cell carries 2×2 sub-pixels; the glyph fills the "bright" quadrants in
# the foreground colour and shows the rest in the background — twice the detail
# of a half-block at the same size.  Keyed by (TL, TR, BL, BR) bright-mask.
_QUAD_GLYPHS = {
    (1, 0, 0, 0): "▘", (0, 1, 0, 0): "▝", (0, 0, 1, 0): "▖", (0, 0, 0, 1): "▗",
    (1, 1, 0, 0): "▀", (0, 0, 1, 1): "▄", (1, 0, 1, 0): "▌", (0, 1, 0, 1): "▐",
    (1, 0, 0, 1): "▚", (0, 1, 1, 0): "▞", (1, 1, 1, 0): "▛", (1, 1, 0, 1): "▜",
    (1, 0, 1, 1): "▙", (0, 1, 1, 1): "▟", (1, 1, 1, 1): "█",
}


def _art_text_quad(pixels: list, px: int) -> Text:
    """Flat RGBA pixels (2px wide × px tall) → a px-wide, px/2-tall block of
    quadrant glyphs: 2×2 sub-pixels per cell, so twice the horizontal detail."""
    w = px * 2
    t = Text()
    rows = px // 2
    for cy in range(rows):
        if cy:
            t.append("\n")
        y0, y1 = cy * 2, cy * 2 + 1
        for cx in range(px):
            x0, x1 = cx * 2, cx * 2 + 1
            quad = []                       # TL, TR, BL, BR
            for (x, y) in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                i = (y * w + x) * 4
                quad.append((pixels[i], pixels[i + 1], pixels[i + 2]))
            # split the 4 sub-pixels into two colour groups along the channel
            # with the widest spread — keeps real colour contrast, not just
            # brightness (a red mark on green stays sharp).
            ch  = max(range(3), key=lambda k: max(c[k] for c in quad)
                      - min(c[k] for c in quad))
            vals = [c[ch] for c in quad]
            mid  = (max(vals) + min(vals)) / 2
            mask = tuple(1 if v >= mid else 0 for v in vals)
            if mask == (1, 1, 1, 1) or mask == (0, 0, 0, 0):
                r = sum(c[0] for c in quad) // 4
                g = sum(c[1] for c in quad) // 4
                b = sum(c[2] for c in quad) // 4
                t.append("█", style=f"#{r:02x}{g:02x}{b:02x}")
                continue
            fg = [c for c, m in zip(quad, mask) if m]
            bg = [c for c, m in zip(quad, mask) if not m]
            fr = sum(c[0] for c in fg) // len(fg)
            fg_ = sum(c[1] for c in fg) // len(fg)
            fb = sum(c[2] for c in fg) // len(fg)
            br = sum(c[0] for c in bg) // len(bg)
            bg_ = sum(c[1] for c in bg) // len(bg)
            bb = sum(c[2] for c in bg) // len(bg)
            top = f"#{fr:02x}{fg_:02x}{fb:02x}"
            bot = f"#{br:02x}{bg_:02x}{bb:02x}"
            t.append(_QUAD_GLYPHS[mask], style=f"{top} on {bot}")
    return t


def _art_placeholder(px: int) -> Text:
    """What a tile shows until (or instead of) its artwork: a quiet block."""
    rows = px // 2
    return Text("\n".join("▒" * px for _ in range(rows)), style="dim")


def _art_url(item: dict) -> str | None:
    """Best artwork URL for a track / playlist / user."""
    if _item_kind(item) == "user":
        return item.get("avatar_url")
    url = item.get("artwork_url")
    if not url and _item_kind(item) == "playlist":
        for tr in item.get("tracks") or []:
            if tr.get("artwork_url"):
                url = tr["artwork_url"]
                break
    return url or (item.get("user") or {}).get("avatar_url")


def _fetch_wave(track: dict, width: int) -> list[int] | None:
    """Fetch the real waveform of a track (the .png URL has a .json twin)."""
    try:
        url = (track.get("waveform_url") or "").replace(".png", ".json")
        if not url.endswith(".json"):
            return None
        r = _pw_ctx.request.get(url)
        if not r.ok:
            return None
        j = r.json()
        return _downsample_wave(j.get("samples") or [], j.get("height") or 140, width)
    except Exception:
        return None


# -----------------------------------------------------------------------------
#  UI
# -----------------------------------------------------------------------------

console = Console()

# same drill as veltui: the ascii logo took longer than the audio pipeline
_LOGO = r"""
 _  __ _       _    _  _   ___    _____  _   _  ___
| |/ /| |     /_\  | \| | / __|  |_   _|| | | ||_ _|
| ' < | |__  / _ \ | .` || (_ |    | |  | |_| | | |
|_|\_\|____|/_/ \_\|_|\_| \___|    |_|   \___/ |___|
"""

COMMANDS: list[tuple[str, str]] = [
    ("/help",            "show this help"),
    ("/keys",            "keyboard shortcuts"),
    ("/search <text>",   "search it all — tracks, playlists, people (typing works too)"),
    ("/search tracks <text>", "narrow the search: tracks · playlists · people"),
    ("/discover",        "soundcloud's front page — charts & curated selections"),
    ("/home",            "back to the front page (Esc from an empty prompt too)"),
    ("/open <url>",      "open a soundcloud.com link (track/playlist/artist)"),
    ("/play <n>",        "play item n — playlists open & play, people open"),
    ("/play",            "resume playback"),
    ("/pause",           "pause (Ctrl+P toggles)"),
    ("/next",            "next track in the queue"),
    ("/prev",            "previous track in the queue"),
    ("/queue",           "show the queue"),
    ("/queue <n>",       "jump to queue item n"),
    ("/add <n>",         "add item n from the last list to the queue"),
    ("/seek <m:ss|±s>",  "seek — /seek 1:30 · /seek +15 · /seek -15"),
    ("/volume <0-100>",  "set the volume"),
    ("/radio [n]",       "endless radio from the playing track (or item n)"),
    ("/shuffle",         "shuffle the rest of the queue"),
    ("/repeat [mode]",   "repeat: off · all · one"),
    ("/np",              "now-playing details (click the player bar too)"),
    ("/like [n]",        "like the playing track (or item n from the list)"),
    ("/unlike [n]",      "remove a like"),
    ("/likes",           "your liked tracks"),
    ("/playlists",       "your playlists & albums"),
    ("/playlist <n>",    "open playlist n from /playlists"),
    ("/profile",         "your profile"),
    ("/login",           "sign in (opens a Firefox window — log in there)"),
    ("/logout",          "sign out"),
    ("/theme",           "list color themes"),
    ("/theme <n|name>",  "switch color theme"),
    ("/zoom <+|-|n>",    "resize playlist cards (+ bigger · - smaller · reset)"),
    ("/pixels <quad|half>", "artwork detail — crisp 2x2 quadrants or simple blocks"),
    ("/clear",           "clear the screen"),
    ("/exit",            "quit klangtui"),
]

# Unique command words (first token of each entry) for Tab-completion + the
# live suggestion box. Keeps the first description seen for each word.
_CMD_INFO: list[tuple[str, str]] = []
_seen_cmd: set[str] = set()
for _c, _d in COMMANDS:
    _word = _c.split()[0]
    if _word not in _seen_cmd:
        _seen_cmd.add(_word)
        _CMD_INFO.append((_word, _d))


# A suggestion item is a 4-tuple consumed by the live menu:
#   (fill, label, desc, swatch) — see veltui for the original of this scheme
SuggestItem = tuple[str, str, str, str | None]


def _arg_suggestions(lead: str, word: str, arg: str) -> list[SuggestItem]:
    """Choices for the *argument* of a command (e.g. `/theme <here>`)."""
    arg = arg.strip()
    if " " in arg:                      # past the first argument token — stop
        return []
    al = arg.lower()
    items: list[SuggestItem] = []
    if word == "theme":
        for name, d in _THEME_DEFS.items():
            if name.startswith(al):
                items.append((f"{lead}theme {name} ", name, "", d["accent"]))
    elif word == "search":
        for sc in ("tracks", "playlists", "people"):
            if al and sc.startswith(al):
                items.append((f"{lead}search {sc} ", sc, f"only {sc}", None))
    elif word == "volume":
        for v in ("25", "50", "75", "100"):
            if v.startswith(al):
                items.append((f"{lead}volume {v} ", v, "", None))
    elif word == "repeat":
        for mode, desc in (("off", "play the queue once"),
                           ("all", "loop the whole queue"),
                           ("one", "loop the current track")):
            if mode.startswith(al):
                items.append((f"{lead}repeat {mode} ", mode, desc, None))
    elif word in ("zoom", "size"):
        for val, desc in (("+", "bigger cards"),
                          ("-", "smaller cards"),
                          ("reset", "default size"),
                          ("8", "tiny"),
                          ("16", "default"),
                          ("24", "large"),
                          ("40", "huge — sharpest art")):
            if not al or val.startswith(al):
                items.append((f"{lead}{word} {val} ", val, desc, None))
    elif word in ("pixels", "art"):
        for val, desc in (("quad", "crisp 2x2 — more detail"),
                          ("half", "simple 1x2 blocks")):
            if not al or val.startswith(al):
                items.append((f"{lead}{word} {val} ", val, desc, None))
    return items


def _suggestions_for(value: str) -> list[SuggestItem]:
    """Live-menu items for the current input value (commands, then arguments)."""
    if not value.startswith(("/", ":")):
        return []
    lead  = value[0]
    after = value[1:]
    if " " in after:                    # command word is done — suggest its argument
        word, arg = after.split(" ", 1)
        return _arg_suggestions(lead, word.lower(), arg)
    body = after.lower()                # still typing the command word itself
    return [
        (lead + name[1:] + " ", name, desc, None)
        for (name, desc) in _CMD_INFO
        if name[1:].lower().startswith(body)
    ]


def _help_panel():
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", pad_edge=False)
    t.add_column("command",     style="bold green", min_width=24)
    t.add_column("description", style="dim")
    for cmd, desc in COMMANDS:
        t.add_row(cmd, desc)
    return Panel(
        t,
        title=f"[bold]commands[/bold] · {len(COMMANDS)} total",
        border_style="cyan",
        padding=(0, 1),
    )


_KEYS: list[tuple[str, str]] = [
    ("Tab",     "autocomplete commands · walk into the page"),
    ("↑ / ↓",   "input history / menu · move between results"),
    ("← / →",   "move between cards"),
    ("Enter",   "send · play the focused card or track"),
    ("Esc",     "back to the prompt · home · cancel sign-in"),
    ("mouse",   "click a card/track · click the nav · click the player bar"),
    ("Ctrl+P",  "play / pause"),
    ("Ctrl+N",  "next track"),
    ("Ctrl+B",  "previous track"),
    ("Ctrl+T",  "next color theme"),
    ("Ctrl+L",  "clear the screen"),
    ("Ctrl+Q",  "quit (/exit works too)"),
]


def _keys_panel():
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", pad_edge=False)
    t.add_column("key",    style="bold green", min_width=24)
    t.add_column("action", style="dim")
    for key, action in _KEYS:
        t.add_row(key, action)
    return Panel(
        t,
        title="[bold]keyboard shortcuts[/bold]",
        border_style="cyan",
        padding=(0, 1),
    )


def _profile_panel(user: dict):
    t = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    t.add_column("field", style="dim", min_width=12)
    t.add_column("value", style="bold")
    rows = [
        ("name",      user.get("full_name") or user.get("username") or "?"),
        ("username",  "@" + (user.get("permalink") or "?")),
        ("followers", _fmt_count(user.get("followers_count"))),
        ("following", _fmt_count(user.get("followings_count"))),
        ("tracks",    _fmt_count(user.get("track_count"))),
        ("playlists", _fmt_count(user.get("playlist_count"))),
        ("likes",     _fmt_count(user.get("likes_count"))),
        ("city",      ", ".join(x for x in (user.get("city"),
                                            user.get("country_code")) if x)),
        ("url",       user.get("permalink_url") or ""),
    ]
    for k, v in rows:
        if v:
            t.add_row(k, str(v))
    return Panel(t, title="[bold]your profile[/bold]", border_style="cyan", padding=(0, 1))


def _np_panel(track: dict, state: dict | None, qpos: str):
    t = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    t.add_column("field", style="dim", min_width=12)
    t.add_column("value", style="bold")
    pos = ""
    if state:
        pos = f"{_fmt_time(state.get('t'))} / {_fmt_time(state.get('d') or _track_secs(track))}"
    rows = [
        ("title",    track.get("title") or "?"),
        ("artist",   _track_artist(track)),
        ("position", pos),
        ("queue",    qpos),
        ("genre",    track.get("genre") or ""),
        ("plays",    _fmt_count(track.get("playback_count"))),
        ("likes",    _fmt_count(track.get("likes_count"))),
        ("url",      track.get("permalink_url") or ""),
    ]
    for k, v in rows:
        if v:
            t.add_row(k, str(v))
    return Panel(t, title="[bold]now playing[/bold]", border_style="cyan", padding=(0, 1))


# -----------------------------------------------------------------------------
#  Color themes (same palette engine as veltui)
# -----------------------------------------------------------------------------

# klangtui's own palette — music-named, and the default is SoundCloud-orange
_THEME_DEFS: dict[str, dict] = {
    "ember":  dict(primary="#e25d2b", accent="#ff7a3d", background="#190d07",
                   surface="#2a160c", panel="#361d10", foreground="#f6e7dd"),
    "vinyl":  dict(primary="#c9b896", accent="#e6d5b0", background="#131110",
                   surface="#1f1c19", panel="#2a2622", foreground="#efe9df"),
    "neon":   dict(primary="#e84393", accent="#ff5fb0", background="#120714",
                   surface="#211026", panel="#2c1533", foreground="#f5e3f2"),
    "aurora": dict(primary="#3ec97e", accent="#62e8a0", background="#07130d",
                   surface="#102019", panel="#152a21", foreground="#e0f0e8"),
    "ocean":  dict(primary="#4f8cff", accent="#74a6ff", background="#0a0f1f",
                   surface="#131a2e", panel="#1b243d", foreground="#e3e9f5"),
    "grape":  dict(primary="#9d7cff", accent="#b89bff", background="#13101f",
                   surface="#1e1830", panel="#28203f", foreground="#ece6f7"),
    "mono":   dict(primary="#cfcfd6", accent="#f5f5f7", background="#08080a",
                   surface="#161618", panel="#202023", foreground="#ededf0"),
    "haze":   dict(primary="#8a93a6", accent="#aab4c8", background="#16181c",
                   surface="#20242b", panel="#2a2f37", foreground="#e6e9ef"),
}

DEFAULT_THEME = "ember"


def _build_theme(name: str, d: dict) -> Theme:
    return Theme(
        name=name,
        primary=d["primary"],
        accent=d["accent"],
        background=d["background"],
        surface=d["surface"],
        panel=d["panel"],
        foreground=d["foreground"],
        success="#4caf78",
        warning="#e0a13a",
        error="#ff5f6b",
        dark=True,
    )


def _theme_accent(name: str) -> str:
    return _THEME_DEFS.get(name, _THEME_DEFS[DEFAULT_THEME])["accent"]


# -----------------------------------------------------------------------------
#  App (Textual TUI — logo on top, scrolling feed, player bar + input at bottom)
# -----------------------------------------------------------------------------

_SC_URL_RE = re.compile(r"https?://(?:www\.|m\.|on\.)?soundcloud\.com/\S+")

DEFAULT_VOLUME = 80
DEFAULT_CARD_PX = 16          # playlist / profile card artwork size (columns)
MIN_CARD_PX = 8
MAX_CARD_PX = 40
_CARD_STEP = 2
_BAR_WIDTH = 40


class ItemWidget(Static):
    """A focusable, clickable result bound to a SoundCloud object.

    Enter or a mouse click activates it (play / open). Views number their
    widgets, so `/play <n>` always matches what's on screen.
    """

    can_focus = True
    ART_PX = 20            # artwork size in pixels == terminal columns
    QUAD = False           # subclasses opt into crisp 2×2 quadrant artwork

    def __init__(self, item: dict, index: int, *, playing: bool = False):
        super().__init__()
        self.item    = item
        self.index   = index
        self.playing = playing
        self.art: Text | None = None

    def on_mount(self):
        self.update(self._renderable())

    def set_art(self, art: Text):
        self.art = art
        self.update(self._renderable())

    def _renderable(self):
        raise NotImplementedError

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.app._activate_item(self.item)

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            self.app._activate_item(self.item)
        elif event.key == "right":
            event.stop()
            self.screen.focus_next()
        elif event.key == "left":
            event.stop()
            self.screen.focus_previous()

    def on_focus(self, event: events.Focus) -> None:
        self.update(self._renderable())

    def on_blur(self, event: events.Blur) -> None:
        self.update(self._renderable())


class BackButton(Static):
    """A clickable '← back' at the top of every page below the front one."""

    can_focus = True

    def on_mount(self):
        t = Text()
        t.append("← back", style="bold")
        t.append("  (Esc)", style="dim")
        self.update(t)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.app._go_back_view()

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            self.app._go_back_view()
        elif event.key in ("right", "down"):
            event.stop()
            self.screen.focus_next()
        elif event.key in ("left", "up"):
            event.stop()
            self.screen.focus_previous()


class Card(ItemWidget):
    """A square tile with artwork — the site's playlist/profile card, in glyphs."""

    ART_PX = 20
    QUAD = True               # crisp 2×2 quadrant artwork

    def _renderable(self):
        it   = self.item
        kind = _item_kind(it)
        if kind == "user":
            title = it.get("username") or "?"
            fol   = _fmt_count(it.get("followers_count"))
            sub   = f"{fol} followers" if fol else "profile"
        else:
            title = it.get("title") or "?"
            n     = it.get("track_count") or len(it.get("tracks") or [])
            by    = (it.get("user") or {}).get("username") or "soundcloud"
            sub   = f"{n} trk · {by}" if n else by
        head = Text(no_wrap=True, overflow="ellipsis")
        head.append(f"{self.index} ", style="dim")
        head.append(title, style="bold")
        return Group(
            self.art or _art_placeholder(self.ART_PX),
            head,
            Text(sub, style="dim", no_wrap=True, overflow="ellipsis"),
        )


class TrackRow(ItemWidget):
    """A track row: thumbnail, title, artist, length — the site's track list."""

    ART_PX = 8

    def _renderable(self):
        tr   = self.item
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=self.ART_PX)
        grid.add_column(no_wrap=True, overflow="ellipsis")
        title = Text(no_wrap=True, overflow="ellipsis")
        if self.playing:
            title.append("> ", style="bold")
        elif self.has_focus:
            title.append("· ", style="bold")
        title.append(f"{self.index} ", style="dim")
        title.append(tr.get("title") or "?", style="bold")
        meta = Text(no_wrap=True, style="dim")
        meta.append(_fmt_time(_track_secs(tr)))
        plays = _fmt_count(tr.get("playback_count"))
        if plays:
            meta.append(f" · {plays} plays")
        grid.add_row(
            self.art or _art_placeholder(self.ART_PX),
            Group(title,
                  Text(_track_artist(tr), style="dim", no_wrap=True,
                       overflow="ellipsis"),
                  meta),
        )
        return grid


class NavButton(Static):
    """A clickable item in the top nav bar (home · library · likes · profile)."""

    can_focus = False        # mouse-first; keyboard nav stays on cards/input

    def __init__(self, label: str, *, action: str):
        super().__init__()
        self.label_text = label
        self.action_name = action
        self.active = False

    def on_mount(self):
        self._repaint()

    def set_active(self, active: bool):
        if active != self.active:
            self.active = active
            self._repaint()

    def _repaint(self):
        self.update(Text(self.label_text,
                         style="bold" if self.active else "dim"))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.app._nav_to(self.action_name)


class VolumeBar(Static):
    """A little volume mixer in the top-right of the nav bar — click (or drag)
    anywhere on the bar to set the level."""

    can_focus = False        # mouse-first; keyboard nav stays on cards/input
    _x0 = 4                  # where the bar starts (after the "vol " label)
    _bar_w = 0               # bar width in columns (set when rendered)

    def on_mount(self):
        self.app._refresh_volume()

    def on_resize(self, event: events.Resize) -> None:
        # re-render once the real width is known (and on every resize) so the
        # bar is aligned from the start, not only after the first click
        self.app._refresh_volume()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        bw = self._bar_w or max(1, self.size.width - 4)
        self.app._apply_volume(round((event.x - self._x0) / bw * 100))


class NowBar(Static):
    """The bottom player bar — click it to open the full track page."""

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.app._open_now_playing()


class ArtView(Static):
    """The track-page cover — real artwork in crisp 2×2 quadrant glyphs."""

    ART_PX = 18
    QUAD = True               # crisp 2×2 quadrant artwork

    def __init__(self, item: dict):
        super().__init__()
        self.item = item
        self.art: Text | None = None

    def on_mount(self):
        self.update(self.art or _art_placeholder(self.ART_PX))

    def set_art(self, art: Text):
        self.art = art
        self.update(art)


class ActionButton(Static):
    """A small clickable action on the track page (like · radio · play/pause)."""

    can_focus = True

    def __init__(self, label: str, callback):
        super().__init__()
        self.label_text = label
        self.callback = callback

    def on_mount(self):
        self.update(Text(self.label_text, style="bold"))

    def set_label(self, label: str):
        self.label_text = label
        self.update(Text(label, style="bold"))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.callback()

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            self.callback()
        elif event.key == "right":
            event.stop()
            self.screen.focus_next()
        elif event.key == "left":
            event.stop()
            self.screen.focus_previous()


class SeekBar(Static):
    """The track-page scrubber: the real waveform with times at each end,
    click on the wave to seek."""

    can_focus = True
    _x0 = 0                   # column where the waveform starts (after the time)
    _wave_w = 0               # waveform width in columns

    def on_click(self, event: events.Click) -> None:
        event.stop()
        ww = self._wave_w or self.size.width
        if ww > 0:
            frac = (event.x - self._x0) / ww
            self.app._seek_fraction(max(0.0, min(1.0, frac)))

    def on_key(self, event: events.Key) -> None:
        if event.key == "right":
            event.stop()
            self.screen.focus_next()
        elif event.key == "left":
            event.stop()
            self.screen.focus_previous()


class KlangtuiTUI(App):
    TITLE = "klangtui"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: $background;
    }
    #content {
        padding: 0 2 1 2;
    }
    BackButton {
        height: 1;
        width: 18;
        margin: 1 0 0 0;
        padding: 0 1;
        background: $surface;
    }
    BackButton:focus {
        background: $panel;
        text-style: bold;
    }
    .section-title {
        height: 1;
        margin: 1 0 0 0;
    }
    .cardrow {
        height: 15;
        margin: 0 0 1 0;
        scrollbar-size-horizontal: 1;
    }
    Card {
        width: 24;
        height: 14;
        padding: 0 1;
        margin: 0 1 0 0;
        background: $surface;
        border: round $surface;
    }
    Card:focus {
        background: $panel;
        border: round $accent;
    }
    TrackRow {
        height: 4;
        padding: 0 1;
        margin: 0 0 1 0;
        background: $surface;
    }
    TrackRow:focus {
        background: $panel;
    }
    #topbar {
        dock: top;
        height: 3;
        margin: 1 2 0 2;
        background: $surface;
    }
    NavButton {
        width: auto;
        height: 3;
        padding: 1 2;
        color: $foreground;
    }
    NavButton:hover {
        background: $panel;
        text-style: bold;
    }
    #topsearch {
        width: 1fr;
        height: 3;
        margin: 0 1 0 1;
        border: round $accent;
        background: $surface;
    }
    #volbar {
        width: 22;
        height: 3;
        content-align: left middle;
        padding: 0 1;
    }
    #volbar:hover {
        background: $panel;
    }
    #bottombar {
        dock: bottom;
        height: auto;
    }
    #now {
        height: auto;
        margin: 0 2 0 2;
        padding: 0 1;
        border: round $accent;
        background: $surface;
    }
    #now:hover {
        border: round $primary;
    }
    #prompt {
        height: 3;
        margin: 0 2 1 2;
        border: round $accent;
        background: $surface;
    }
    #suggest {
        height: auto;
        max-height: 14;
        margin: 0 2 0 2;    /* pops up just above the command input */
        padding: 0 1;
        background: $panel;
        border: round $accent;
        display: none;
    }
    .actionrow {
        height: 1;
        margin: 1 0 0 0;
    }
    ActionButton {
        width: auto;
        height: 1;
        padding: 0 2;
        margin: 0 1 0 0;
        background: $surface;
        color: $foreground;
        content-align: center middle;
    }
    ActionButton:hover {
        background: $panel;
    }
    ActionButton:focus {
        background: $accent;
        color: $background;
        text-style: bold;
    }
    SeekBar {
        height: 1;
        margin: 1 0 1 0;
    }
    SeekBar:focus {
        background: $panel;
    }
    .detailtop {
        height: auto;
        margin: 0 0 0 0;
    }
    ArtView {
        width: 18;
        height: auto;
        margin: 0 2 0 0;
    }
    ArtView:focus {
        background: $panel;
    }
    .detailinfo {
        width: 1fr;
        height: auto;
    }
    .detailmeta {
        height: auto;
        margin: 0 0 1 0;
    }
    .msg {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+l", "clear_feed", "clear", show=False),
        Binding("ctrl+t", "cycle_theme", "theme", show=False),
        Binding("ctrl+p", "toggle_play", "play/pause", show=False),
        Binding("ctrl+n", "next_track", "next", show=False),
        Binding("ctrl+b", "prev_track", "prev", show=False),
        Binding("escape", "go_back", "back", show=False),
        Binding("tab", "complete", "complete", show=False, priority=True),
        Binding("up", "history_prev", "prev", show=False, priority=True),
        Binding("down", "history_next", "next", show=False, priority=True),
    ]

    def __init__(self, start_query: str | None = None, autoconnect: bool = True):
        super().__init__()
        self.db = DB()
        saved_theme = self.db.get("theme", DEFAULT_THEME)
        self._theme_name = saved_theme if saved_theme in _THEME_DEFS else DEFAULT_THEME
        try:
            self._volume = max(0, min(100, int(self.db.get("volume", str(DEFAULT_VOLUME)))))
        except ValueError:
            self._volume = DEFAULT_VOLUME
        try:
            self._card_px = max(MIN_CARD_PX, min(MAX_CARD_PX,
                                int(self.db.get("card_px", str(DEFAULT_CARD_PX)))))
        except ValueError:
            self._card_px = DEFAULT_CARD_PX
        # crisp 2×2 quadrant artwork (vs the simpler 1×2 half-block)
        self._art_quad = self.db.get("art_quad", "1") != "0"
        self._start_query = start_query
        self._autoconnect = autoconnect
        self.backend = SCBackend(
            on_tick=lambda st: self.call_from_thread(self._on_tick, st),
            on_ended=lambda: self.call_from_thread(self._on_ended),
        )
        self._busy = False
        self._user: dict | None = None        # /me payload while signed in
        # the numbered list currently on screen (search results / likes / playlist)
        self._listing: list[dict] = []
        self._playlists: list[dict] = []
        # play queue
        self._queue: list[dict] = []
        self._qi = -1
        # now-playing state
        self._now: dict | None = None         # the track loaded in the player
        self._now_state: dict | None = None   # last tick {t,d,p,e}
        self._now_status: str | None = None   # "resolving stream…" etc.
        self._snipped = False
        self._wave: list[int] | None = None   # the track's real waveform, in glyph levels
        self._repeat = "off"                  # off · all · one
        self._liked_ids: set = set()          # likes toggled this session (marker)
        # artwork cache (rendered Text) + widgets waiting for a fetch in flight
        self._art_texts: dict = {}
        self._art_waiting: dict = {}
        # view history — ← back / Esc rebuild previous pages from cached data
        self._view_stack: list = []
        self._current_view = None
        self._nav_back = False
        # top-nav active section
        self._section: str | None = None
        # the track page's live widgets (seek bar / like / play-pause buttons)
        self._seekbar = None
        self._detail_track: dict | None = None
        self._detail_like_btn = None
        self._detail_pp_btn = None
        # command-suggestion state (Tab / ↑↓ move through these)
        self._suggest_items: list[SuggestItem] = []
        self._suggest_index = -1
        self._pending_completions = 0
        # input history (↑/↓ recall past searches & commands, like a shell)
        self._history: list[str] = []
        self._history_index = 0
        self._history_draft = ""

    # --- layout ---

    def compose(self) -> ComposeResult:
        # top: nav buttons, a quick-search box, then the volume mixer
        with Horizontal(id="topbar"):
            yield NavButton("home", action="home")
            yield NavButton("library", action="library")
            yield NavButton("likes", action="likes")
            yield NavButton("profile", action="profile")
            yield Input(placeholder="search soundcloud…", id="topsearch")
            yield VolumeBar(id="volbar")
        yield VerticalScroll(id="content")
        # bottom bar: player on top, the command input at the very bottom, with the
        # suggestion menu popping up between them
        with Vertical(id="bottombar"):
            yield NowBar(self._now_renderable(), id="now")
            yield Static(id="suggest")
            yield Input(placeholder=self._prompt_placeholder(), id="prompt")

    @staticmethod
    def _prompt_placeholder() -> str:
        return "type to search · /help for commands"

    def on_mount(self):
        for name, d in _THEME_DEFS.items():
            self.register_theme(_build_theme(name, d))
        self.theme = self._theme_name
        self.content = self.query_one("#content", VerticalScroll)
        self.query_one("#prompt", Input).focus()
        self.content.mount(Static(self._welcome_renderable(), classes="msg"))
        self._refresh_volume()
        if self._autoconnect:
            self._connect()

    def _welcome_renderable(self):
        accent = _theme_accent(self._theme_name)
        return Group(
            Text(_LOGO.strip("\n"), style=f"bold {accent}"),
            Text(),
            Text.assemble(("* ", f"bold {accent}"), ("welcome to klangtui", "bold")),
            Text("type anything to search soundcloud · click a card or /play <n> · "
                 "/help for commands", style="dim"),
        )

    def on_unmount(self):
        self.backend.shutdown()
        self.backend._thread.join(timeout=5)

    def _refresh_logo(self):
        """Account state changed — repaint the player bar and nav."""
        self._refresh_now()
        self._refresh_nav()

    # --- top nav bar ---

    def _nav_to(self, action: str):
        if self._busy:
            return
        if action == "home":
            self._cmd_discover()
        elif action == "library":
            self._cmd_playlists()
        elif action == "likes":
            self._cmd_likes()
        elif action == "profile":
            self._cmd_profile()

    def _set_section(self, name: str | None):
        self._section = name
        self._refresh_nav()

    def _refresh_nav(self):
        try:
            buttons = list(self.query(NavButton))
        except Exception:
            return
        for b in buttons:
            b.set_active(b.action_name == self._section)

    # --- the volume mixer in the nav bar ---

    def _volume_renderable(self, bar):
        accent = _theme_accent(self._theme_name)
        total  = bar.size.width or 22
        label, suffix = "vol ", f" {self._volume:>3}%"
        bw = max(4, total - len(label) - len(suffix))
        bar._x0, bar._bar_w = len(label), bw
        filled = round(bw * self._volume / 100)
        filled = max(0, min(bw, filled))
        t = Text(no_wrap=True)
        t.append(label, style="dim")
        t.append("█" * filled, style=f"bold {accent}")
        t.append("─" * (bw - filled), style="dim")
        t.append(suffix, style="dim")
        return t

    def _refresh_volume(self):
        try:
            bar = self.query_one("#volbar", VolumeBar)
        except Exception:
            return
        bar.update(self._volume_renderable(bar))

    def _apply_volume(self, v: int):
        """Set the absolute volume (clamped) and sync the player + mixer."""
        v = max(0, min(100, int(v)))
        if v == self._volume:
            return
        self._volume = v
        self.db.put("volume", str(self._volume))
        self._quiet_job("volume", value=self._volume)
        self._refresh_now()
        self._refresh_volume()

    # --- the content area: views, toasts, artwork ---

    def _clear_content(self):
        # any track-page widgets are about to be removed — drop our references
        self._seekbar = None
        self._detail_track = None
        self._detail_like_btn = None
        self._detail_pp_btn = None
        self.content.remove_children()

    def _show_loading(self, label: str) -> Static:
        """Replace the view with a one-line loading status; returns the widget."""
        self._clear_content()
        w = Static(Text(f"·  {label}", style="dim"), classes="msg")
        self.content.mount(w)
        return w

    def _add_system(self, renderable):
        """Old feed habit, new manners: short Texts pop up as toasts, rich
        panels (help, profile, …) take over the content area."""
        if isinstance(renderable, Text):
            blob = str(renderable.style or "") + " ".join(
                str(s.style) for s in renderable.spans
            )
            severity = ("error" if "red" in blob
                        else "warning" if "yellow" in blob else "information")
            self.notify(renderable.plain, severity=severity, timeout=4)
        else:
            self._view_panel(renderable)

    def _view_panel(self, renderable):
        self._push_view(lambda: self._view_panel(renderable))
        self._clear_content()
        self._mount_back()
        self.content.mount(Static(renderable, classes="msg"))
        self.content.scroll_home(animate=False)

    def _scroll_top(self):
        self.call_after_refresh(self.content.scroll_home, animate=False)

    def _set_busy(self, busy: bool):
        self._busy = busy
        for wid in ("#prompt", "#topsearch"):
            try:
                self.query_one(wid, Input).disabled = busy
            except Exception:
                pass
        if not busy:
            self.query_one("#prompt", Input).focus()

    def _cb(self, fn):
        """Wrap a callback so the worker thread lands it on the UI thread."""
        return lambda *a: self.call_from_thread(fn, *a)

    # --- artwork: progressive pop-in, cached per (url, size) ---

    def _request_art(self, widgets: list):
        for w in widgets:
            url = _art_url(w.item)
            if not url:
                continue
            quad = self._art_quad and getattr(w, "QUAD", False)
            key = (url, w.ART_PX, quad)
            if key in self._art_texts:
                w.set_art(self._art_texts[key])
                continue
            waiting = self._art_waiting.setdefault(key, [])
            waiting.append(w)
            if len(waiting) > 1:        # a fetch for this art is already queued
                continue
            self.backend.submit("art", url=url, px=w.ART_PX, quad=quad,
                                on_done=self._cb(self._art_done),
                                on_error=self._cb(lambda e: None))

    def _art_done(self, res: dict):
        quad = res.get("quad", False)
        key = (res["url"], res["px"], quad)
        waiting = self._art_waiting.pop(key, [])
        if not res["pixels"]:
            return
        art = (_art_text_quad if quad else _art_text)(res["pixels"], res["px"])
        self._art_texts[key] = art
        for w in waiting:
            if w.is_mounted:
                w.set_art(art)

    # --- views (the site's pages, in widgets) + back navigation ---

    def _push_view(self, rebuild):
        """Remember how to rebuild the *current* page before a new one replaces
        it, so ← back / Esc can walk the history (data comes from the caches —
        no new requests)."""
        if self._nav_back:
            self._current_view = rebuild
            return
        if self._current_view is not None:
            self._view_stack.append(self._current_view)
            del self._view_stack[:-10]          # a short memory is plenty
        self._current_view = rebuild

    def _go_back_view(self):
        if self._busy:
            return
        if not self._view_stack:
            self._cmd_discover()                # bottom of the stack — go home
            return
        rebuild = self._view_stack.pop()
        self._nav_back = True
        try:
            rebuild()
        finally:
            self._nav_back = False

    def _mount_back(self):
        if self._view_stack:
            self.content.mount(BackButton())

    def _section_title(self, text: str) -> Static:
        accent = _theme_accent(self._theme_name)
        return Static(Text(text, style=f"bold {accent}"), classes="section-title")

    # --- card sizing (the /zoom command resizes playlist & profile cards) ---

    def _card_dims(self) -> tuple[int, int, int]:
        """(card width, card height, cardrow height) for the current zoom."""
        px = self._card_px
        art_rows = (px + 1) // 2                 # half-block art is 2 px per row
        card_h = art_rows + 4                    # art + title + subtitle + pad
        return px + 4, card_h, card_h + 1

    def _make_card(self, item: dict, index: int) -> "Card":
        """A Card sized to the current /zoom level."""
        card = Card(item, index)
        card.ART_PX = self._card_px
        cw, ch, _ = self._card_dims()
        card.styles.width = cw
        card.styles.height = ch
        return card

    def _size_cardrow(self, row) -> None:
        row.styles.height = self._card_dims()[2]

    def _rerender_view(self):
        """Rebuild the page now on screen in place (no new history entry)."""
        if self._current_view is None:
            return
        self._nav_back = True
        try:
            self._current_view()
        finally:
            self._nav_back = False

    def _view_home(self, sections: list[dict]):
        self._push_view(lambda: self._view_home(sections))
        self._clear_content()
        flat: list[dict] = []
        widgets = []
        for sec in sections:
            self.content.mount(self._section_title(sec["title"]))
            row = HorizontalScroll(classes="cardrow")
            self.content.mount(row)
            self._size_cardrow(row)
            for pl in sec["playlists"]:
                flat.append(pl)
                card = self._make_card(pl, len(flat))
                row.mount(card)
                widgets.append(card)
        self._listing = flat
        self._request_art(widgets)
        self._scroll_top()

    def _view_tracks(self, title: str, tracks: list[dict], *,
                     mark_index: int | None = None, more: int = 0):
        self._push_view(lambda: self._view_tracks(title, tracks,
                                                  mark_index=mark_index, more=more))
        self._clear_content()
        self._mount_back()
        self.content.mount(self._section_title(f"{title} · {len(tracks)}"))
        widgets = []
        for i, tr in enumerate(tracks):
            row = TrackRow(tr, i + 1, playing=(mark_index == i))
            self.content.mount(row)
            widgets.append(row)
        if more:
            self.content.mount(Static(Text(f"+{more} more not loaded", style="dim"),
                                      classes="msg"))
        if not tracks:
            self.content.mount(Static(Text("nothing here", style="dim"), classes="msg"))
        self._listing = list(tracks)
        self._request_art(widgets)
        self._scroll_top()

    def _view_cards(self, title: str, items: list[dict]):
        """A wrapped grid of cards — the library page."""
        self._push_view(lambda: self._view_cards(title, items))
        self._clear_content()
        self._mount_back()
        self.content.mount(self._section_title(f"{title} · {len(items)}"))
        widgets = []
        for start in range(0, len(items), 4):
            row = HorizontalScroll(classes="cardrow")
            self.content.mount(row)
            self._size_cardrow(row)
            for j, it in enumerate(items[start:start + 4]):
                card = self._make_card(it, start + j + 1)
                row.mount(card)
                widgets.append(card)
        if not items:
            self.content.mount(Static(Text("nothing here", style="dim"), classes="msg"))
        self._listing = list(items)
        self._request_art(widgets)
        self._scroll_top()

    def _view_search(self, q: str, items: list[dict]):
        """The site's mixed search page: cards on top, track rows below."""
        self._push_view(lambda: self._view_search(q, items))
        self._clear_content()
        self._mount_back()
        self._listing = list(items)
        cards = [(i, it) for i, it in enumerate(items) if _item_kind(it) != "track"]
        rows  = [(i, it) for i, it in enumerate(items) if _item_kind(it) == "track"]
        widgets = []
        if cards:
            self.content.mount(self._section_title(f"search: {q} — playlists & people"))
            cardrow = HorizontalScroll(classes="cardrow")
            self.content.mount(cardrow)
            self._size_cardrow(cardrow)
            for i, it in cards:
                card = self._make_card(it, i + 1)
                cardrow.mount(card)
                widgets.append(card)
        if rows:
            self.content.mount(self._section_title(f"search: {q} — tracks"))
            for i, it in rows:
                row = TrackRow(it, i + 1)
                self.content.mount(row)
                widgets.append(row)
        if not items:
            self.content.mount(Static(Text("nothing found", style="dim"), classes="msg"))
        self._request_art(widgets)
        self._scroll_top()

    def _activate_item(self, item: dict):
        """What a click / Enter on a result does — the site's row/card click."""
        if self._busy:
            return
        kind = _item_kind(item)
        if kind == "playlist":
            self._open_playlist_item(item, autoplay=True)
        elif kind == "user":
            self._open_user_item(item)
        else:
            tracks = [it for it in self._listing if _item_kind(it) == "track"]
            if item in tracks:
                self._play_tracks(tracks, tracks.index(item))
            else:
                self._play_tracks([item], 0)

    # --- the track page (click the player bar to open it) ---

    def _open_now_playing(self):
        if self._busy:
            return
        if self._now is None:
            self._add_system(Text("nothing playing — /play <n> first", style="dim"))
            return
        if self._detail_track is not None:   # already on the track page
            return
        self._view_track_detail(self._now)

    def _reopen_detail(self):
        """Rebuild the open track page for the current track, without growing the
        back stack (used when /next, /prev or auto-advance change the track)."""
        if self._now is None:
            return
        self._nav_back = True
        try:
            self._view_track_detail(self._now)
        finally:
            self._nav_back = False

    def _view_track_detail(self, track: dict):
        """The full track page.  All the controls — the waveform scrubber and
        the transport buttons — sit at the very top, so they're reachable
        without scrolling at *any* window size (down to a tiny tiling-WM
        square).  The cover and details follow below."""
        self._set_section(None)
        self._push_view(lambda: self._view_track_detail(track))
        self._clear_content()
        self._mount_back()
        self._detail_track = track
        qpos = f"{self._qi + 1} of {len(self._queue)}" if self._queue else ""
        liked  = track.get("id") in self._liked_ids
        paused = bool(self._now_state and self._now_state.get("p"))
        # top block: a small cover on the left; title, artist and the transport
        # buttons stacked to its right — compact, so it all stays on screen
        art = ArtView(track)
        art.ART_PX = 14
        art.styles.width = 14
        head = Static(self._detail_head(track), classes="detailmeta")
        self._detail_pp_btn = ActionButton(
            "▶" if paused else "||", self.action_toggle_play)
        self._detail_like_btn = ActionButton(
            "unlike" if liked else "like", lambda: self._detail_like(track))
        transport = Horizontal(
            ActionButton("<< prev", self._cmd_prev),
            self._detail_pp_btn,
            ActionButton("next >>", self._cmd_next),
            classes="actionrow")
        extras = Horizontal(
            ActionButton("vol -", lambda: self._detail_volume(-10)),
            ActionButton("vol +", lambda: self._detail_volume(+10)),
            self._detail_like_btn,
            ActionButton("radio", lambda: self._cmd_radio("")),
            classes="actionrow")
        right = Vertical(head, transport, extras, classes="detailinfo")
        self.content.mount(Horizontal(art, right, classes="detailtop"))
        # the waveform scrubber, full width, right under the controls
        seek = SeekBar()
        self._seekbar = seek
        self.content.mount(seek)
        # the rest of the track's details, below
        details = self._detail_stats(track, qpos)
        if details is not None:
            self.content.mount(Static(details, classes="detailmeta"))
        self._request_art([art])
        self.call_after_refresh(self._refresh_seekbar)
        self._scroll_top()

    def _detail_head(self, track: dict):
        """Title + artist — shown to the right of the cover, above the buttons."""
        accent = _theme_accent(self._theme_name)
        return Group(
            Text(track.get("title") or "?", style=f"bold {accent}",
                 no_wrap=True, overflow="ellipsis"),
            Text(_track_artist(track), style="bold",
                 no_wrap=True, overflow="ellipsis"),
        )

    def _detail_stats(self, track: dict, qpos: str):
        """The fuller track info shown below the scrubber (None if there's none)."""
        lines = []
        stats = []
        for val, label in ((track.get("playback_count"), "plays"),
                           (track.get("likes_count"), "likes")):
            c = _fmt_count(val)
            if c:
                stats.append(f"{c} {label}")
        if track.get("genre"):
            stats.append(track["genre"])
        if stats:
            lines.append(Text("  ·  ".join(stats), style="dim"))
        if self._snipped:
            lines.append(Text("30s preview (Go+)", style="yellow"))
        if qpos:
            lines.append(Text(f"queue: {qpos}", style="dim"))
        if track.get("permalink_url"):
            lines.append(Text(track["permalink_url"], style="dim",
                              no_wrap=True, overflow="ellipsis"))
        return Group(*lines) if lines else None

    def _detail_like(self, track: dict):
        self._do_like(track, track.get("id") not in self._liked_ids)

    def _detail_volume(self, delta: int):
        self._apply_volume(self._volume + delta)

    # --- the clickable scrubber on the track page ---

    def _seekbar_renderable(self, bar):
        accent = _theme_accent(self._theme_name)
        total  = bar.size.width or 60
        st  = self._now_state
        dur = (st.get("d") if st else 0) or _track_secs(self._now or {})
        t   = (st.get("t", 0) if st else 0)
        pos_s, dur_s = _fmt_time(t), _fmt_time(dur)
        left, right = f"{pos_s} ", f" {dur_s}"
        width = max(4, total - len(left) - len(right))
        bar._x0, bar._wave_w = len(left), width      # so a click maps to the wave
        filled = int(width * t / dur) if dur else 0
        filled = max(0, min(width, filled))
        out = Text(no_wrap=True)
        out.append(left, style="dim")
        if self._wave:
            w = self._wave
            glyphs = "".join(_WAVE_GLYPHS[w[i * len(w) // width]]
                             for i in range(width))
            out.append(glyphs[:filled], style=f"bold {accent}")
            out.append(glyphs[filled:], style="dim")
        else:
            out.append("━" * filled, style=accent)
            out.append("─" * max(0, width - filled), style="dim")
        out.append(right, style="dim")
        return out

    def _refresh_seekbar(self):
        bar = self._seekbar
        if bar is None or not bar.is_mounted:
            return
        bar.update(self._seekbar_renderable(bar))

    def _seek_fraction(self, frac: float):
        if self._now is None:
            return
        st  = self._now_state
        dur = (st.get("d") if st else 0) or _track_secs(self._now)
        if not dur:
            return
        self._quiet_job("seek", rel=False, value=frac * dur)

    def _refresh_detail_like(self):
        btn, track = self._detail_like_btn, self._detail_track
        if btn is not None and btn.is_mounted and track is not None:
            liked = track.get("id") in self._liked_ids
            btn.set_label("unlike" if liked else "like")

    def _refresh_detail_pp(self):
        btn = self._detail_pp_btn
        if btn is not None and btn.is_mounted:
            paused = bool(self._now_state and self._now_state.get("p"))
            btn.set_label("▶" if paused else "||")

    # --- the now-playing bar ---

    def _eq_glyphs(self, paused: bool) -> str:
        """A tiny three-bar equalizer that dances on every tick while playing."""
        if paused:
            return "▁▁▁"
        beat = int(time.time() * 2.5)
        return "".join(
            _WAVE_GLYPHS[1 + hash((beat, i, self._qi)) % 6] for i in range(3)
        )

    def _now_renderable(self):
        accent = _theme_accent(self._theme_name)
        if self._now is None and not self._now_status:
            idle = Text()
            idle.append("nothing playing — type to search · ",
                        style="dim")
            if self._user:
                idle.append("account: ", style="dim")
                idle.append(self._user.get("username") or "you", style="bold green")
            else:
                idle.append("account: ", style="dim")
                idle.append("guest", style="bold yellow")
                idle.append(" · /login", style="dim")
            return idle
        if self._now is None:
            return Text(f"·  {self._now_status}", style="dim")
        tr    = self._now
        liked = tr.get("id") in self._liked_ids
        head  = Text()
        st    = self._now_state
        paused = bool(st and st.get("p"))
        head.append(("|| " if paused else "> "), style=f"bold {accent}")
        head.append(self._eq_glyphs(paused) + "  ", style=accent)
        head.append(tr.get("title") or "?", style="bold")
        head.append("  —  ", style="dim")
        head.append(_track_artist(tr))
        if liked:
            head.append("  *liked", style="bold red")
        if self._snipped:
            head.append("  · 30s preview (Go+)", style="yellow")
        if self._queue:
            head.append(f"   · {self._qi + 1}/{len(self._queue)}", style="dim")
        if self._repeat != "off":
            head.append(f"  loop {self._repeat}", style="dim")
        if self._now_status:
            return Group(head, Text(f"·  {self._now_status}", style="dim"))
        dur = (st.get("d") if st else 0) or _track_secs(tr)
        t   = st.get("t", 0) if st else 0
        filled = int(_BAR_WIDTH * t / dur) if dur else 0
        filled = max(0, min(_BAR_WIDTH, filled))
        bar = Text()
        bar.append(_fmt_time(t), style="dim")
        bar.append(" ")
        if self._wave:
            # the track's actual waveform — played part lights up in the accent
            glyphs = "".join(_WAVE_GLYPHS[v] for v in self._wave)
            bar.append(glyphs[:filled], style=f"bold {accent}")
            bar.append(glyphs[filled:], style="dim")
        else:
            bar.append("━" * filled, style=accent)
            bar.append("o", style=f"bold {accent}")
            bar.append("─" * max(0, _BAR_WIDTH - filled - 1), style="dim")
        bar.append(" ")
        bar.append(_fmt_time(dur), style="dim")
        bar.append(f"   vol {self._volume}%", style="dim")
        bar.append("   · click to open", style="dim")
        return Group(head, bar)

    def _refresh_now(self):
        self.query_one("#now", Static).update(self._now_renderable())

    def _on_tick(self, state: dict):
        self._now_state = state
        if not self._now_status:        # don't fight a "loading…" line
            self._refresh_now()
        self._refresh_seekbar()         # live scrubber on the open track page
        self._refresh_detail_pp()

    def _on_ended(self):
        if self._repeat == "one":
            self._play_index(self._qi, auto=True)
        elif self._qi + 1 < len(self._queue):
            self._play_index(self._qi + 1, auto=True)
        elif self._repeat == "all" and self._queue:
            self._play_index(0, auto=True)
        else:
            self._now_state = dict(self._now_state or {}, p=True)
            self._now_status = None
            self._refresh_now()
            self._add_system(Text("queue finished — /repeat all loops it", style="dim"))

    # --- connecting ---

    def _connect(self):
        w = self._show_loading("connecting to SoundCloud…")

        def status(s):
            w.update(Text(f"·  {s}", style="dim"))

        def done(res):
            self._user = res.get("user")
            self._refresh_logo()
            if self._user:
                name = self._user.get("username") or "you"
                self.notify(f"signed in as {name}", timeout=4)
            else:
                self.notify("connected as guest — /login for likes & playlists",
                            timeout=5)
            # land on the front page, like the site (or run the -q search)
            if self._start_query:
                q, self._start_query = self._start_query, None
                self._do_search(q)
            else:
                self._cmd_discover()

        def err(e):
            w.update(Text(f"error: {e}", style="bold red"))
            self.notify(str(e), severity="error", timeout=6)

        self.backend.submit("connect", on_status=self._cb(status),
                            on_done=self._cb(done), on_error=self._cb(err))

    # --- busy view jobs (search / likes / playlists / …) ---

    def _run_view(self, kind: str, label: str, build, **kw):
        """Submit a job whose result becomes the next view; input locks meanwhile."""
        self._set_busy(True)
        w = self._show_loading(label)

        def status(s):
            w.update(Text(f"·  {s}", style="dim"))

        def done(res):
            self._set_busy(False)
            build(res)

        def err(e):
            w.update(Text(f"error: {e}", style="bold red"))
            self.notify(str(e), severity="error", timeout=5)
            self._set_busy(False)

        self.backend.submit(kind, on_status=self._cb(status),
                            on_done=self._cb(done), on_error=self._cb(err), **kw)

    def _do_search(self, q: str, scope: str = "all"):
        self._set_section(None)
        label = q + (f" · {scope}" if scope != "all" else "")

        def build(items):
            self._view_search(label, items)
        self._run_view("search", f"searching: {q}…", build, q=q, scope=scope)

    def _cmd_discover(self):
        self._set_section("home")
        self._run_view("discover", "loading discover…", self._view_home)

    def _open_playlist_item(self, pl: dict, autoplay: bool = False):
        def build(res):
            mark = 0 if (autoplay and res["tracks"]) else None
            self._view_tracks(res["title"], res["tracks"], more=res["more"],
                              mark_index=mark)
            if autoplay and res["tracks"]:
                self._play_tracks(res["tracks"], 0)
        self._run_view("playlist_tracks",
                       f"loading {pl.get('title') or 'playlist'}…", build, playlist=pl)

    def _open_user_item(self, user: dict):
        name = user.get("username") or "?"

        def build(tracks):
            self._view_tracks(f"tracks by {name}", tracks)
        self._run_view("user_tracks", f"loading tracks by {name}…", build, user=user)

    def _do_open(self, url: str):
        def build(res):
            if res["kind"] == "track":
                self._view_tracks("opened link", [res["track"]])
                self._play_tracks([res["track"]], 0)
                return
            self._view_tracks(res["title"], res["tracks"], more=res.get("more", 0))
        self._run_view("open_url", "resolving link…", build, url=url)

    # --- playback (never locks the input — jobs serialize in the worker) ---

    def _play_tracks(self, tracks: list[dict], index: int):
        self._queue = list(tracks)
        self._play_index(index)

    def _play_index(self, i: int, auto: bool = False):
        if not (0 <= i < len(self._queue)):
            return
        self._qi = i
        track = self._queue[i]
        self._now = track
        self._now_state = None
        self._snipped = False
        self._wave = None
        self._now_status = "loading…"
        self._refresh_now()
        if self._detail_track is not None:   # keep the open track page in sync
            self._reopen_detail()

        def status(s):
            self._now_status = s
            self._refresh_now()

        def done(res):
            self._now = res["track"]
            self._queue[i] = res["track"]
            self._snipped = res["snipped"]
            self._wave = res.get("wave")
            self._now_status = None
            self._refresh_now()
            if res["snipped"]:
                self._add_system(Text(
                    "only a 30-second preview is available for this track (Go+)",
                    style="yellow",
                ))

        def err(e):
            self._now_status = None
            self._refresh_now()
            self._add_system(Text(
                f"can't play “{track.get('title') or '?'}”: {e}", style="bold red"
            ))
            if auto and self._qi + 1 < len(self._queue):
                self._play_index(self._qi + 1, auto=True)   # skip to the next one

        self.backend.submit("play", track=track, volume=self._volume,
                            bar_width=_BAR_WIDTH,
                            on_status=self._cb(status), on_done=self._cb(done),
                            on_error=self._cb(err))

    def _quiet_job(self, kind: str, ok=None, **kw):
        """Fire a small player job; errors land in the feed, success is silent."""
        def err(e):
            self._add_system(Text(str(e), style="red"))

        self.backend.submit(kind, on_done=self._cb(ok) if ok else None,
                            on_error=self._cb(err), **kw)

    # --- input handling ---

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "topsearch":      # the quick-search box in the navbar
            text = event.value.strip()
            event.input.value = ""
            if text and not self._busy:
                self._dispatch_input(text)
            return
        if event.input.id != "prompt":
            return
        text = event.value.strip()
        event.input.value = ""
        if not text or self._busy:
            return
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_index = len(self._history)
        self._history_draft = ""
        self._dispatch_input(text)

    def _dispatch_input(self, text: str):
        """Run a submitted line: a /command, a soundcloud.com link, or a search."""
        if text.startswith(("/", ":")):
            self._handle_command(text)
        elif _SC_URL_RE.match(text):
            self._do_open(text)
        else:
            self._do_search(text)

    # --- command autocomplete (same engine as veltui) ---

    def on_input_changed(self, event: Input.Changed):
        if event.input.id != "prompt":
            return
        if self._pending_completions > 0:   # this edit came from Tab — don't reset
            self._pending_completions -= 1
            return
        self._suggest_index = -1
        self._refresh_suggest(event.value)

    def _refresh_suggest(self, value: str):
        box_w = self.query_one("#suggest", Static)
        items = _suggestions_for(value)
        self._suggest_items = items
        if items:
            box_w.update(self._suggest_renderable(items, self._suggest_index))
            box_w.display = True
        else:
            box_w.display = False
            self._suggest_index = -1

    _SUGGEST_WINDOW = 8     # how many rows of the menu are visible at once

    def _suggest_renderable(self, items, index=-1):
        accent = _theme_accent(self._theme_name)
        n = len(items)
        win = self._SUGGEST_WINDOW
        # scroll a window of rows so the highlighted item is always on screen
        if n <= win or index < 0:
            start = 0
        else:
            start = min(max(0, index - win // 2), n - win)
        end = min(n, start + win)
        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True)   # label
        grid.add_column(no_wrap=True)   # color swatch (themes only)
        grid.add_column()               # description
        for i in range(start, end):
            _fill, label, desc, swatch = items[i]
            chip = Text("████", style=swatch) if swatch else Text("")
            if i == index:
                grid.add_row(Text(f"> {label}", style="bold"), chip, Text(desc),
                             style=f"black on {accent}")
            else:
                grid.add_row(Text(f"  {label}", style="bold green"), chip,
                             Text(desc, style="dim"))
        parts = [grid]
        crumbs = []
        if start > 0:
            crumbs.append(f"↑ {start} more")
        if end < n:
            crumbs.append(f"↓ {n - end} more")
        if crumbs:
            parts.append(Text("   ".join(crumbs), style=f"bold {accent}"))
        pos  = f"{index + 1}/{n} · " if index >= 0 else f"{n} · "
        hint = (f"{pos}↑↓ or Tab to move · Enter to run" if n > 1
                else "Tab to complete · Enter to run")
        parts.append(Rule(style="dim"))
        parts.append(Text(hint, style="dim italic"))
        return Group(*parts)

    def action_complete(self):
        """Tab — move through the command menu, or walk into the results."""
        if isinstance(self.focused, ItemWidget):
            self.screen.focus_next()
            return
        if self._suggest_items:
            self._move_suggest(1)
            return
        items = self.query(ItemWidget)      # empty prompt → Tab enters the page
        if items:
            items.first().focus()

    def _move_suggest(self, delta: int):
        items = self._suggest_items
        if not items:
            return
        if self._suggest_index == -1:
            self._suggest_index = 0 if delta > 0 else len(items) - 1
        else:
            self._suggest_index = (self._suggest_index + delta) % len(items)
        fill = items[self._suggest_index][0]
        inp  = self.query_one("#prompt", Input)
        if fill != inp.value:
            self._pending_completions += 1   # this edit is ours — don't treat as typing
            inp.value = fill
            inp.cursor_position = len(fill)
        # Completing the *only* command match commits it, so step the menu into
        # that command's argument choices (e.g. "/theme " → the list of themes).
        if len(items) == 1:
            self._suggest_index = -1
            self._refresh_suggest(fill)
        else:
            box_w = self.query_one("#suggest", Static)
            box_w.update(self._suggest_renderable(items, self._suggest_index))
            box_w.display = True

    # --- input history (↑/↓) ---

    def action_history_prev(self):
        if isinstance(self.focused, ItemWidget):
            self.screen.focus_previous()    # browsing the page → ↑ moves focus
        elif self._suggest_items:           # menu open → move the highlight up
            self._move_suggest(-1)
        else:                               # menu closed → recall an earlier line
            self._recall_history(-1)

    def action_history_next(self):
        if isinstance(self.focused, ItemWidget):
            self.screen.focus_next()
        elif self._suggest_items:
            self._move_suggest(+1)
        else:
            self._recall_history(+1)

    def action_go_back(self):
        """Escape — clear the focused input, or leave the page."""
        foc = self.focused
        if isinstance(foc, Input):          # clear it, or go back if already empty
            if foc.value:
                foc.value = ""
            else:
                self._go_back_view()
        else:
            self.query_one("#prompt", Input).focus()

    def _recall_history(self, delta: int):
        if self._busy or not self._history:
            return
        foc = self.focused          # history belongs to the bottom command input
        if not (isinstance(foc, Input) and foc.id == "prompt"):
            return
        inp = self.query_one("#prompt", Input)
        if self._history_index >= len(self._history):
            self._history_draft = inp.value     # stash the in-progress line
        new = max(0, min(self._history_index + delta, len(self._history)))
        if new == self._history_index:
            return
        self._history_index = new
        text = self._history_draft if new == len(self._history) else self._history[new]
        if text != inp.value:
            self._pending_completions += 1
            inp.value = text
        inp.cursor_position = len(text)

    # --- key actions ---

    def action_clear_feed(self):
        if not self._busy:
            self._cmd_clear()

    def action_toggle_play(self):
        if self._now is None:
            return

        def done(state):
            self._now_state = dict(self._now_state or {}, p=(state == "paused"))
            self._refresh_now()

        self._quiet_job("toggle", ok=done)

    def action_next_track(self):
        self._cmd_next()

    def action_prev_track(self):
        self._cmd_prev()

    def action_cycle_theme(self):
        names = list(_THEME_DEFS)
        i = (names.index(self._theme_name) + 1) % len(names) \
            if self._theme_name in names else 0
        self._apply_theme(names[i])
        self._add_system(
            Text.assemble(("theme: ", "green"), (names[i], "bold"), ("   (Ctrl+T)", "dim"))
        )

    # --- commands ---

    def _handle_command(self, line: str):
        if line.startswith(":"):
            line = "/" + line[1:]
        raw   = line[1:]
        parts = raw.split(None, 1)
        cmd   = parts[0].lower()
        args  = parts[1] if len(parts) > 1 else ""

        if cmd in ("help", "h", "?"):
            self._add_system(_help_panel())
        elif cmd in ("keys", "shortcuts", "hotkeys"):
            self._add_system(_keys_panel())
        elif cmd in ("search", "s", "find"):
            a = args.strip()
            if not a:
                self._add_system(Text(
                    "usage: /search <text>   ·   /search tracks|playlists|people <text>",
                    style="dim"))
            else:
                first, _, rest = a.partition(" ")
                scopes = {"tracks": "tracks", "playlists": "playlists",
                          "people": "people", "users": "people"}
                if first.lower() in scopes and rest.strip():
                    self._do_search(rest.strip(), scope=scopes[first.lower()])
                else:
                    self._do_search(a)
        elif cmd in ("discover", "home"):
            self._cmd_discover()
        elif cmd == "open":
            if _SC_URL_RE.match(args.strip()):
                self._do_open(args.strip())
            else:
                self._add_system(Text("usage: /open <soundcloud.com link>", style="dim"))
        elif cmd in ("play", "p"):
            self._cmd_play(args)
        elif cmd == "pause":
            self._cmd_pause()
        elif cmd in ("resume", "unpause"):
            self._cmd_resume()
        elif cmd in ("next", "n", "skip"):
            self._cmd_next()
        elif cmd in ("prev", "previous", "back"):
            self._cmd_prev()
        elif cmd in ("queue", "q"):
            self._cmd_queue(args)
        elif cmd == "add":
            self._cmd_add(args)
        elif cmd == "seek":
            self._cmd_seek(args)
        elif cmd in ("volume", "vol", "v"):
            self._cmd_volume(args)
        elif cmd == "radio":
            self._cmd_radio(args)
        elif cmd == "shuffle":
            self._cmd_shuffle()
        elif cmd == "repeat":
            self._cmd_repeat(args)
        elif cmd == "np":
            self._cmd_np()
        elif cmd == "like":
            self._cmd_like(args, like=True)
        elif cmd == "unlike":
            self._cmd_like(args, like=False)
        elif cmd == "likes":
            self._cmd_likes()
        elif cmd == "playlists":
            self._cmd_playlists()
        elif cmd == "playlist":
            self._cmd_playlist(args)
        elif cmd == "profile":
            self._cmd_profile()
        elif cmd == "login":
            self._cmd_login(args)
        elif cmd == "logout":
            self._cmd_logout()
        elif cmd == "theme":
            self._cmd_theme(args)
        elif cmd in ("zoom", "size"):
            self._cmd_zoom(args)
        elif cmd in ("pixels", "art"):
            self._cmd_pixels(args)
        elif cmd == "clear":
            self._cmd_clear()
        elif cmd in ("exit", "quit"):
            self.exit()
        else:
            self._add_system(
                Text(f"unknown command: /{cmd}   (try /help)", style="red")
            )

    # numbers in commands refer to the numbered list currently on screen
    def _listing_item(self, arg: str) -> dict | None:
        try:
            idx = int(arg.strip()) - 1
        except ValueError:
            self._add_system(Text("that's not a number — use the # from the list",
                                  style="red"))
            return None
        if not self._listing:
            self._add_system(Text("no list on screen — search first", style="red"))
            return None
        if not (0 <= idx < len(self._listing)):
            self._add_system(Text(f"pick a number between 1 and {len(self._listing)}",
                                  style="red"))
            return None
        return self._listing[idx]

    def _listing_track(self, arg: str, *, action: str) -> dict | None:
        """Like _listing_item, but the item must be a track."""
        item = self._listing_item(arg)
        if item is None:
            return None
        kind = _item_kind(item)
        if kind != "track":
            self._add_system(Text(
                f"#{arg.strip()} is a {kind} — only tracks can be {action} "
                "( /play <n> opens it )", style="red"))
            return None
        return item

    def _cmd_play(self, args: str):
        arg = args.strip()
        if not arg:
            if self._now is not None:
                self._cmd_resume()
            else:
                self._add_system(Text(
                    "nothing loaded — search, then /play <n>", style="dim"))
            return
        if _SC_URL_RE.match(arg):
            self._do_open(arg)
            return
        item = self._listing_item(arg)
        if item is None:
            return
        kind = _item_kind(item)
        if kind == "playlist":              # like the site: play opens the playlist
            self._open_playlist_item(item, autoplay=True)
            return
        if kind == "user":                  # …and a person opens their tracks
            self._open_user_item(item)
            return
        tracks = [it for it in self._listing if _item_kind(it) == "track"]
        self._play_tracks(tracks, tracks.index(item))

    def _cmd_pause(self):
        if self._now is None:
            self._add_system(Text("nothing is playing", style="dim"))
            return

        def done(_):
            self._now_state = dict(self._now_state or {}, p=True)
            self._refresh_now()

        self._quiet_job("pause", ok=done)

    def _cmd_resume(self):
        if self._now is None:
            self._add_system(Text("nothing is playing", style="dim"))
            return

        def done(_):
            self._now_state = dict(self._now_state or {}, p=False)
            self._refresh_now()

        self._quiet_job("resume", ok=done)

    def _cmd_next(self):
        if self._qi + 1 < len(self._queue):
            self._play_index(self._qi + 1)
        else:
            self._add_system(Text("end of the queue", style="dim"))

    def _cmd_prev(self):
        if self._qi - 1 >= 0 and self._queue:
            self._play_index(self._qi - 1)
        else:
            self._add_system(Text("already at the start of the queue", style="dim"))

    def _cmd_queue(self, args: str):
        arg = args.strip()
        if not arg:
            if not self._queue:
                self._add_system(Text("the queue is empty — /play <n> starts one",
                                      style="dim"))
                return
            self._view_tracks("queue", self._queue, mark_index=self._qi)
            return
        try:
            idx = int(arg) - 1
        except ValueError:
            self._add_system(Text("usage: /queue   ·   /queue <n>", style="dim"))
            return
        if not (0 <= idx < len(self._queue)):
            self._add_system(Text(f"pick a number between 1 and {len(self._queue)}",
                                  style="red"))
            return
        self._play_index(idx)

    def _cmd_add(self, args: str):
        track = self._listing_track(args, action="queued")
        if track is None:
            return
        self._queue.append(track)
        self._add_system(Text.assemble(
            ("queued: ", "green"), (track.get("title") or "?", "bold"),
            (f"   ({len(self._queue)} in queue)", "dim"),
        ))
        if self._now is None:           # nothing playing — start with it
            self._play_index(len(self._queue) - 1)

    def _cmd_seek(self, args: str):
        spec = args.strip()
        m = re.fullmatch(r"([+-]?)(?:(\d+):)?(\d+(?:\.\d+)?)", spec)
        if not m:
            self._add_system(Text(
                "usage: /seek 1:30   ·   /seek 45   ·   /seek +15   ·   /seek -15",
                style="dim"))
            return
        sign, mins, secs = m.groups()
        value = (int(mins) * 60 if mins else 0) + float(secs)
        if sign == "-":
            value = -value
        self._quiet_job("seek", rel=bool(sign), value=value)

    def _cmd_volume(self, args: str):
        arg = args.strip().rstrip("%")
        if not arg:
            self._add_system(Text(f"volume: {self._volume}%   ( /volume <0-100> )",
                                  style="dim"))
            return
        try:
            v = int(arg)
        except ValueError:
            self._add_system(Text("usage: /volume <0-100>", style="dim"))
            return
        self._apply_volume(v)
        self._add_system(Text.assemble(("volume: ", "green"),
                                       (f"{self._volume}%", "bold")))

    def _cmd_radio(self, args: str):
        arg = args.strip()
        if arg:
            seed = self._listing_track(arg, action="seeded")
            if seed is None:
                return
        elif self._now is not None:
            seed = self._now
        else:
            self._add_system(Text("play something first, or /radio <n> seeds from the list",
                                  style="dim"))
            return
        seed_playing = self._now is not None and self._now.get("id") == seed.get("id")

        def build(tracks):
            if not tracks:
                self.notify("the radio found nothing similar", timeout=4)
                self._cmd_queue("")
                return
            title = f"radio: {seed.get('title') or '?'}"
            if seed_playing:
                # keep the current track playing, replace everything after it
                self._queue = self._queue[:self._qi + 1] + tracks
                self._refresh_now()
                self._view_tracks(title, self._queue, mark_index=self._qi)
            else:
                self._play_tracks([seed] + tracks, 0)
                self._view_tracks(title, self._queue, mark_index=0)

        self._run_view("related", "tuning the radio…", build, track=seed)

    def _cmd_shuffle(self):
        upcoming = len(self._queue) - (self._qi + 1)
        if upcoming < 2:
            self._add_system(Text("nothing to shuffle — the queue is (almost) over",
                                  style="dim"))
            return
        rest = self._queue[self._qi + 1:]
        random.shuffle(rest)
        self._queue[self._qi + 1:] = rest
        self._refresh_now()
        self._add_system(Text.assemble(
            ("shuffled ", "green"), (str(upcoming), "bold"), (" upcoming tracks", "green"),
        ))

    def _cmd_repeat(self, args: str):
        arg = args.strip().lower()
        modes = ("off", "all", "one")
        if not arg:                      # bare /repeat cycles through the modes
            arg = modes[(modes.index(self._repeat) + 1) % len(modes)]
        if arg not in modes:
            self._add_system(Text("usage: /repeat off · /repeat all · /repeat one",
                                  style="dim"))
            return
        self._repeat = arg
        self._refresh_now()
        self._add_system(Text.assemble(("repeat: ", "green"), (arg, "bold")))

    def _cmd_np(self):
        if self._now is None:
            self._add_system(Text("nothing is playing", style="dim"))
            return
        qpos = f"{self._qi + 1} of {len(self._queue)}" if self._queue else ""
        self._add_system(_np_panel(self._now, self._now_state, qpos))

    def _cmd_like(self, args: str, *, like: bool):
        arg = args.strip()
        if arg:
            track = self._listing_track(arg, action="liked")
            if track is None:
                return
        elif self._now is not None:
            track = self._now
        else:
            self._add_system(Text("nothing is playing — /like <n> likes a list item",
                                  style="dim"))
            return
        self._do_like(track, like)

    def _do_like(self, track: dict, like: bool):
        """Like / un-like a track and reflect it in the bar and the track page."""
        def done(_):
            if like:
                self._liked_ids.add(track.get("id"))
            else:
                self._liked_ids.discard(track.get("id"))
            self._refresh_now()
            self._refresh_detail_like()
            verb = "liked" if like else "un-liked"
            self._add_system(Text.assemble(
                (f"{verb}: ", "bold red" if like else "green"),
                (track.get("title") or "?", "bold"),
            ))

        self._quiet_job("like" if like else "unlike", track=track, ok=done)

    def _cmd_likes(self):
        self._set_section("likes")

        def build(tracks):
            self._liked_ids.update(t.get("id") for t in tracks)
            self._view_tracks("your likes", tracks)
        self._run_view("likes", "fetching your likes…", build)

    def _cmd_playlists(self):
        self._set_section("library")

        def build(pls):
            self._playlists = pls
            self._view_cards("your playlists", pls)
        self._run_view("playlists", "fetching your library…", build)

    def _cmd_playlist(self, args: str):
        try:
            idx = int(args.strip()) - 1
        except ValueError:
            self._add_system(Text("usage: /playlist <n>   ( /playlists to list them )",
                                  style="dim"))
            return
        if not self._playlists:
            self._add_system(Text("run /playlists first", style="red"))
            return
        if not (0 <= idx < len(self._playlists)):
            self._add_system(Text(f"pick a number between 1 and {len(self._playlists)}",
                                  style="red"))
            return
        self._open_playlist_item(self._playlists[idx])

    def _cmd_profile(self):
        self._set_section("profile")

        def build(user):
            self._user = user
            self._refresh_logo()
            self._view_panel(_profile_panel(user))
        self._run_view("profile", "fetching your profile…", build)

    # --- sign in: opens a Firefox window, you log in on the real site there ---

    def _cmd_login(self, args: str = ""):
        if self._user:
            name = self._user.get("username") or "you"
            self._add_system(Text(
                f"already signed in as {name} — /logout first", style="dim"))
            return
        self._cmd_login_window()

    def _cmd_login_window(self):
        if self._now is not None:
            self.notify("playback stops while the login window is open",
                        severity="warning", timeout=5)
        # the headless browser restarts during login — drop the player state
        self._now = None
        self._now_state = None
        self._now_status = None
        self._refresh_now()
        self._set_busy(True)
        w = self._show_loading("opening the login window…")

        def status(s):
            w.update(Text(f"·  {s}", style="dim"))

        def done(user):
            self._user = user
            self._refresh_logo()
            self.notify(f"signed in as {user.get('username') or 'you'}", timeout=5)
            self._set_busy(False)
            self._cmd_discover()        # back to the front page, now signed in

        def err(e):
            w.update(Text(f"login failed: {e}", style="bold red"))
            self.notify(str(e), severity="error", timeout=6)
            self._set_busy(False)

        self.backend.submit("login", on_status=self._cb(status),
                            on_done=self._cb(done), on_error=self._cb(err))

    def _cmd_logout(self):
        if not self._user:
            self._add_system(Text("you're not signed in", style="dim"))
            return
        self._set_busy(True)

        def done(_):
            self._user = None
            self._refresh_logo()
            self._add_system(Text("signed out — cookies cleared", style="green"))
            self._set_busy(False)

        def err(e):
            self._add_system(Text(f"error: {e}", style="bold red"))
            self._set_busy(False)

        self.backend.submit("logout", on_done=self._cb(done), on_error=self._cb(err))

    def _themes_panel(self):
        t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", pad_edge=False)
        t.add_column("#",       style="dim", width=3)
        t.add_column("theme",   min_width=12)
        t.add_column("preview", width=8)
        for i, (name, d) in enumerate(_THEME_DEFS.items(), 1):
            active = name == self._theme_name
            label  = f"[bold green]{name}[/bold green]" if active else name
            marker = "✓ " if active else "  "
            swatch = f"[{d['accent']}]████[/]"
            t.add_row(str(i), marker + label, swatch)
        return Panel(
            t,
            title="[bold]themes[/bold]   ·   /theme <n|name>   ·   Ctrl+T",
            border_style="cyan",
            padding=(0, 1),
        )

    def _resolve_theme(self, query: str) -> str | None:
        q     = query.strip().lower()
        names = list(_THEME_DEFS)
        if q.isdigit():
            idx = int(q) - 1
            return names[idx] if 0 <= idx < len(names) else None
        if q in _THEME_DEFS:
            return q
        cand = [n for n in names if n.startswith(q)]
        return cand[0] if len(cand) == 1 else None

    def _apply_theme(self, name: str):
        self._theme_name = name
        self.theme = name
        self.db.put("theme", name)
        self._refresh_logo()
        self._refresh_now()
        self._refresh_volume()      # the mixer bakes in the accent colour

    def _cmd_theme(self, args: str):
        if not args.strip():
            self._add_system(self._themes_panel())
            return
        name = self._resolve_theme(args)
        if not name:
            self._add_system(
                Text(f"unknown theme: {args!r}   (use /theme to list)", style="red")
            )
            return
        self._apply_theme(name)
        self._add_system(Text.assemble(("theme: ", "green"), (name, "bold")))

    def _cmd_zoom(self, args: str):
        """Resize the playlist / profile cards.  +/- step, a number sets it."""
        a = args.strip().lower()
        old = self._card_px
        if a in ("", "?"):
            self._add_system(Text(
                f"card size {self._card_px}  ·  /zoom + larger · /zoom - smaller "
                f"· /zoom <{MIN_CARD_PX}-{MAX_CARD_PX}>", style="dim"))
            return
        if a in ("+", "in", "up", "bigger"):
            px = self._card_px + _CARD_STEP
        elif a in ("-", "out", "down", "smaller"):
            px = self._card_px - _CARD_STEP
        elif a in ("reset", "default"):
            px = DEFAULT_CARD_PX
        elif a.isdigit():
            px = int(a)
        else:
            self._add_system(Text(
                "usage: /zoom + · /zoom - · /zoom <n> · /zoom reset", style="red"))
            return
        self._card_px = max(MIN_CARD_PX, min(MAX_CARD_PX, px))
        self.db.put("card_px", str(self._card_px))
        if self._card_px == old:
            if px <= MIN_CARD_PX:
                msg = f"already at the smallest size ({old})"
            elif px >= MAX_CARD_PX:
                msg = f"already at the largest size ({old})"
            else:
                msg = f"card size is already {old}"
            self._add_system(Text(msg, style="dim"))
            return
        self._rerender_view()       # redraw the cards on screen at the new size
        self._add_system(Text.assemble(
            ("card size: ", "green"), (str(self._card_px), "bold")))

    def _cmd_pixels(self, args: str):
        """Crisp 2×2 quadrant artwork vs the simpler 1×2 half-block."""
        a = args.strip().lower()
        if a in ("quad", "fine", "crisp", "hi", "high", "on", "2x2"):
            new = True
        elif a in ("half", "blocky", "low", "off", "1x2"):
            new = False
        elif a in ("", "toggle"):
            new = not self._art_quad
        else:
            self._add_system(Text(
                "usage: /pixels quad  ·  /pixels half", style="red"))
            return
        self._art_quad = new
        self.db.put("art_quad", "1" if new else "0")
        self._rerender_view()       # re-fetch & redraw art in the new mode
        mode = "quad — crisp 2x2" if new else "half-block — simple 1x2"
        self._add_system(Text.assemble(("artwork: ", "green"), (mode, "bold")))

    def _cmd_clear(self):
        self._clear_content()
        self.content.mount(Static(self._welcome_renderable(), classes="msg"))


# -----------------------------------------------------------------------------
#  CLI entry point
# -----------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    epilog_lines = ["in-app commands:"]
    for cmd, desc in COMMANDS:
        epilog_lines.append(f"  {cmd:<26} {desc}")
    epilog_lines.append(
        f"\n  {len(COMMANDS)} commands total  (also :help / /help inside the app)"
    )

    p = argparse.ArgumentParser(
        prog="klangtui",
        description="SoundCloud player in your terminal — search, play, like, playlists",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(epilog_lines),
    )
    p.add_argument(
        "-q", "--query",
        metavar="TEXT",
        help="search for TEXT right after starting",
    )
    p.add_argument(
        "--clear-data", action="store_true",
        help="delete ~/.klangtui (settings + the saved SoundCloud login) and exit",
    )
    p.add_argument(
        "--version", action="version", version="klangtui 0.1.0",
    )
    return p


def main():
    args = _build_parser().parse_args()

    if args.clear_data:
        if DATA_DIR.exists():
            shutil.rmtree(DATA_DIR, ignore_errors=True)
            console.print("[green]cleared ~/.klangtui — settings and login removed[/green]")
        else:
            console.print("[dim]nothing to clear[/dim]")
        sys.exit(0)

    KlangtuiTUI(start_query=args.query).run()


if __name__ == "__main__":
    main()
