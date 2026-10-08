"""klangtui — SoundCloud in your terminal, drawn in your terminal's own colours.

Layout, top to bottom: the tab bar · the now-playing box · the list box · a
one-line footer that turns into the search / command prompt. No themes: the
background is the terminal's own (transparency and all) and every colour is
one of its 16 ANSI colours, so klangtui looks like whatever your terminal does.
"""

from __future__ import annotations

import argparse
import atexit
import random
import re
import shutil
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable

from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from . import __version__, art
from .api import ApiError, AuthError, SoundCloud, artist_of, is_preview, kind_of, seconds_of
from .auth import Session, browser_write, login_window
from .player import MPV_HINT, Mpv, PlayerError, find_mpv
from .radio import build_batch
from .store import Store
from .ui import (ACCENT, CURSOR, DIM, GREEN, RED, TABS, TERMINAL, YELLOW, ListBox, MpvEvent,
                 PlayerBox, PromptInput, Row, TabBar, TextScreen, View, _item_rows, _note,
                 _page_rows, _SC_URL_RE, clean, fmt_time, help_text, info_text, suggestions)

DATA_DIR = Path.home() / ".klangtui"


# --- the app ------------------------------------------------------------------

class Klangtui(App):
    TITLE = "klangtui"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        layout: vertical;
    }
    #top {
        height: 1;
        padding: 0 2;
    }
    TabBar {
        width: 1fr;
    }
    #account {
        width: auto;
    }
    PlayerBox, ListBox {
        margin: 0 1;
        border: solid ansi_bright_black;
        border-title-color: ansi_yellow;
        border-title-style: bold;
        border-subtitle-color: ansi_bright_black;
        border-subtitle-align: right;
    }
    PlayerBox {
        height: 6;
        padding: 0 1;
    }
    ListBox {
        height: 1fr;
        overflow-x: hidden;
        scrollbar-size-vertical: 1;
        scrollbar-color: ansi_bright_black;
        scrollbar-color-hover: ansi_default;
        scrollbar-color-active: ansi_yellow;
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
    }
    ListBox:focus {
        border: solid ansi_default;
    }
    #bottom {
        height: auto;
    }
    #suggest {
        display: none;
        height: auto;
        max-height: 12;
        margin: 0 1;
        padding: 0 1;
        border: solid ansi_bright_black;
        border-title-color: ansi_yellow;
    }
    #footer {
        height: 1;
        padding: 0 2;
    }
    #prefix {
        display: none;
        width: 2;
    }
    PromptInput {
        display: none;
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: ansi_default;
    }
    PromptInput:focus {
        border: none;
    }
    PromptInput > .input--cursor {
        background: ansi_default;
        color: ansi_default;
        text-style: reverse;
    }
    #status {
        width: 1fr;
    }
    #helpkey {
        width: auto;
    }
    """

    BINDINGS = [
        Binding("q", "quit", show=False),
        Binding("ctrl+c", "quit", show=False, priority=True),
        Binding("tab", "cycle_tab(1)", show=False, priority=True),
        Binding("shift+tab", "cycle_tab(-1)", show=False, priority=True),
        Binding("right_square_bracket", "cycle_tab(1)", show=False),
        Binding("left_square_bracket", "cycle_tab(-1)", show=False),
        *[Binding(str(n), f"tab('{name}')", show=False) for n, name in enumerate(TABS, 1)],
        Binding("question_mark", "help", show=False),
        Binding("slash", "prompt('/')", show=False),
        Binding("colon", "prompt(':')", show=False),
        Binding("escape", "back", show=False),
        Binding("space", "toggle_pause", show=False),
        Binding("n", "next", show=False),
        Binding("p", "prev", show=False),
        Binding("left", "seek(-5)", show=False),
        Binding("right", "seek(5)", show=False),
        Binding("shift+left", "seek(-30)", show=False),
        Binding("shift+right", "seek(30)", show=False),
        Binding("plus,equals_sign", "volume(5)", show=False),
        Binding("minus", "volume(-5)", show=False),
        Binding("r", "radio", show=False),
        Binding("l", "like(False)", show=False),
        Binding("L", "like(True)", show=False),
        Binding("a", "enqueue(False)", show=False),
        Binding("A", "enqueue(True)", show=False),
        Binding("d,x", "remove", show=False),
        Binding("s", "shuffle", show=False),
        Binding("m", "repeat", show=False),
        Binding("i", "info", show=False),
        Binding("o", "open_web", show=False),
        Binding("ctrl+r,f5", "reload", show=False),
    ]

    def __init__(self, query: str | None = None):
        super().__init__(ansi_color=True)
        self.store = Store(DATA_DIR / "db.sqlite")
        self.session = Session(DATA_DIR)
        self.api = SoundCloud(DATA_DIR, token=self.session.load())
        self.mpv: Mpv | None = None
        self._mpv_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._start_query = query
        self._shut = False
        # settings
        self.volume = self.store.get_int("volume", 80, 0, 100)
        rep = self.store.get("repeat", "off")
        self.repeat = rep if rep in ("off", "all", "one") else "off"
        self.autoplay = self.store.get("autoplay", "1") != "0"
        self.previews = self.store.get("previews", "0") == "1"
        self.cover_on = self.store.get("cover", "1") != "0"
        # account
        self.user: dict | None = None
        self.liked: set[int] = self.store.liked_ids()
        self._login_cancel: threading.Event | None = None
        # pages
        self.tab = TABS[0]
        self._stacks: dict[str, list[View]] = {t: [] for t in TABS}
        # queue
        self.queue: list[dict] = []
        self.qi = -1
        self.mode = "list"                 # "radio": tops itself up as it plays
        self._epoch = 0                    # bumps whenever the queue is replaced
        self._skipped: set[int] = set()    # skipped within seconds → not a radio seed
        self._radio_busy = False
        self._radio_then_play = False
        # playback
        self.now: dict | None = None
        self._gen = 0                      # bumps on every play request
        self._loaded_gen = -1              # the request mpv is actually playing
        self.position = 0.0
        self.duration = 0.0
        self._max_pos = 0.0
        self.paused = True
        self.buffering = False
        self.preview = False
        self.wave: list[float] | None = None
        self.cover: Text | None = None
        self._status = ""
        self._resume_at = 0.0
        self._source = ""
        self._play_id: int | None = None
        self._fails = 0
        self._loading = False              # a play request is being resolved
        self._at_end = False               # the last track ended; autoplay is looking
        # prompt
        self._prompt_mode = ""
        self._hist = {"/": self.store.get_json("search_history", []) or [],
                      ":": self.store.get_json("command_history", []) or []}
        self._hist_i = 0
        self._draft = ""
        self._sugg: list[tuple[str, str, str]] = []
        self._sugg_i = -1
        self._own_edit = False
        self._msg_timer = None

    # --- layout ---

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield TabBar(id="tabs")
            yield Static(id="account")
        yield PlayerBox(id="player")
        yield ListBox(id="list")
        with Vertical(id="bottom"):
            yield Static(id="suggest")
            with Horizontal(id="footer"):
                yield Static(id="prefix")
                yield PromptInput(id="prompt")
                yield Static(id="status")
                yield Static(Text.assemble(("[?]", ACCENT), " help"), id="helpkey")

    def on_mount(self):
        self.register_theme(TERMINAL)
        self.theme = "terminal"
        self.tabs = self.query_one(TabBar)
        self.player = self.query_one(PlayerBox)
        self.player.border_title = "now playing"
        self.list = self.query_one(ListBox)
        self.prompt = self.query_one(PromptInput)
        self.status = self.query_one("#status", Static)
        self._restore_queue()
        self._refresh_account()
        self._refresh_now()
        self.list.focus()
        self.switch_tab("home")
        if self.api.token:
            self._bg(self._me_job, group="me")
        if self._start_query:
            self.search(self._start_query)
        if not find_mpv():
            self.say(MPV_HINT, "error", 15)

    # --- background work ---

    def _ui(self, fn, *args):
        """Run fn on the UI thread from a worker (no-op once the app is gone)."""
        if self._shut:
            return
        try:
            self.call_from_thread(fn, *args)
        except RuntimeError:
            pass

    def _bg(self, fn, *args, group: str = "bg", exclusive: bool = False):
        """fn(*args) on a worker thread; its errors end up in the footer."""
        def run():
            try:
                fn(*args)
            except ApiError as e:
                self._ui(self.say, str(e), "error")
            except Exception as e:                      # a bug — say so, don't die
                self._ui(self.say, f"internal error: {e!r}", "error", 8)
        self.run_worker(run, thread=True, group=group, exclusive=exclusive,
                        exit_on_error=False)

    def say(self, text: str, kind: str = "info", seconds: float = 4.0):
        """A line in the footer: info · ok · error · dim. 0 s = until replaced."""
        style = {"error": RED, "ok": GREEN, "dim": DIM}.get(kind, Style())
        self.status.update(Text(text, style=style, no_wrap=True, overflow="ellipsis"))
        if self._msg_timer is not None:
            self._msg_timer.stop()
            self._msg_timer = None
        if seconds:
            self._msg_timer = self.set_timer(seconds, lambda: self.status.update(""))

    # --- tabs & pages ---

    def view(self) -> View | None:
        stack = self._stacks[self.tab]
        return stack[-1] if stack else None

    def switch_tab(self, tab: str):
        self.tab = tab
        self.tabs.set_active(tab)
        stack = self._stacks[tab]
        if tab == "queue":
            self._stacks[tab] = [self._queue_view(None)]      # opens on what's playing
        elif tab == "history":
            self._stacks[tab] = [self._history_view(stack[0] if stack else None)]
        elif not stack:
            stack.append(self._root_view(tab))
        self.list.show(self.view(), center=True)

    def _root_view(self, tab: str) -> View:
        if tab == "home":
            v = View("home", "discover")
            self._load(v, self._home_rows)
            return v
        if tab == "search":
            return View("search", "search", _note("press / to search SoundCloud"))
        if not self.user:
            what = "likes" if tab == "likes" else "playlists & albums"
            msg = ("signing in…" if self.api.token
                   else f"sign in to see your {what} — :login")
            return View(tab, tab, _note(msg))
        if tab == "likes":
            v = View("likes", "your likes", more=self._more_likes)
            uid = self.user["id"]
            self._load(v, lambda: self._likes_rows(uid))
            return v
        v = View("library", "your playlists & albums")
        self._load(v, lambda: (_item_rows(self.api.library()), None))
        return v

    def push_view(self, v: View):
        self._stacks[self.tab].append(v)
        self.list.show(v, center=True)

    def action_back(self):
        if self._prompt_mode:
            self._close_prompt()
            return
        if self._login_cancel is not None:
            self._login_cancel.set()
            self.say("cancelling sign-in…", "dim", 0)
            return
        stack = self._stacks[self.tab]
        if len(stack) > 1:
            stack.pop()
            self.list.show(stack[-1])

    def action_tab(self, tab: str):
        self.switch_tab(tab)

    def action_cycle_tab(self, d: int):
        if self._prompt_mode:                   # tab completes inside the prompt
            if d > 0 and self._prompt_mode == ":":
                self._complete()
            return
        if isinstance(self.screen, ModalScreen):
            return
        self.switch_tab(TABS[(TABS.index(self.tab) + d) % len(TABS)])

    def _load(self, v: View, loader: Callable):
        """Fill v from loader() on a worker; v shows 'loading…' meanwhile."""
        v.loader = loader
        v.loading = True
        if not v.rows:
            v.rows = _note("loading…")

        def job():
            try:
                rows, nxt = loader()
            except ApiError as e:
                self._ui(self._view_loaded, v, None, None, str(e))
                return
            except Exception as e:                  # a bug — don't leave it "loading…"
                self._ui(self._view_loaded, v, None, None, f"internal error: {e!r}")
                return
            self._ui(self._view_loaded, v, rows, nxt, None)
        self._bg(job, group="load")

    def _view_loaded(self, v: View, rows, nxt, err):
        v.loading = False
        if err:
            v.rows = _note(f"couldn't load: {err}   · ctrl+r tries again")
            self.say(err, "error")
        else:
            v.rows = rows or _note("nothing here")
            v.next_href = nxt
            if nxt and v.more:
                v.rows.append(Row("more", text="more…"))
        if self.view() is v:
            self.list.show(v)

    def on_list_box_near_end(self, msg: ListBox.NearEnd):
        self._load_more(msg.view)

    def _load_more(self, v: View):
        if v.fetching_more or not v.next_href or not v.more:
            return
        v.fetching_more = True
        href = v.next_href

        def job():
            try:
                rows, nxt = v.more(href)
            except ApiError as e:
                self._ui(self._more_loaded, v, [], href, str(e))
                return
            self._ui(self._more_loaded, v, rows, nxt, None)
        self._bg(job, group="more")

    def _more_loaded(self, v: View, rows, nxt, err):
        v.fetching_more = False
        if err:
            self.say(err, "error")
            return
        if v.rows and v.rows[-1].kind == "more":
            v.rows.pop()
        v.rows.extend(rows)
        v.next_href = nxt
        if nxt:
            v.rows.append(Row("more", text="more…"))
        if self.view() is v:
            self.list.show(v)

    def action_reload(self):
        v = self.view()
        if v and v.loader and not v.loading:
            v.rows, v.next_href = [], None
            self._load(v, v.loader)
            self.list.show(v)
        elif v and v.key in ("queue", "history"):
            self.switch_tab(self.tab)

    # --- loaders (worker side) ---

    def _home_rows(self):
        rows: list[Row] = []
        for sec in self.api.discover():
            rows.append(Row("head", text=sec["title"]))
            rows.extend(Row("playlist", it) for it in sec["items"])
        return rows, None

    def _likes_rows(self, uid: int):
        page = self.api.likes(uid)
        return [Row("track", t) for t in page.items], page.next_href

    def _more_likes(self, href: str):
        page = self.api.more_likes(href)
        return [Row("track", t) for t in page.items], page.next_href

    def _more_items(self, href: str):
        return _page_rows(self.api.more(href))

    # --- live pages: the queue and your history ---

    def _queue_view(self, old: View | None) -> View:
        title = "queue · radio" if self.mode == "radio" else "queue"
        rows = [Row("track", t) for t in self.queue] or _note(
            "the queue is empty — enter on a track starts one, r starts a radio")
        cursor = old.cursor if old else max(0, self.qi)
        return View("queue", title, rows, cursor=cursor)

    def _history_view(self, old: View | None) -> View:
        rows = [Row("track", t, when=ts, count=n) for t, ts, n in self.store.history()]
        return View("history", "history", rows or _note("nothing played yet"),
                    cursor=old.cursor if old else 0)

    def _queue_changed(self):
        if self.tab == "queue":
            self._stacks["queue"] = [self._queue_view(self.view())]
            self.list.show(self.view())
        self._save_state()
        self._refresh_now()

    # --- the selection ---

    def _selected(self) -> tuple[View | None, Row | None]:
        v = self.view()
        if v and 0 <= v.cursor < len(v.rows):
            return v, v.rows[v.cursor]
        return v, None

    def _selected_track(self) -> dict | None:
        _, row = self._selected()
        return row.item if row and row.kind == "track" else None

    def on_list_box_activated(self, msg: ListBox.Activated):
        v, i = msg.view, msg.index
        row = v.rows[i]
        if row.kind == "more":
            self._load_more(v)
        elif row.kind == "playlist":
            self.open_playlist(row.item)
        elif row.kind == "user":
            self.open_user(row.item)
        elif row.kind == "track":
            if v.key == "queue":
                self.play_index(i, source="queue")
                return
            tracks = [r.item for r in v.rows if r.kind == "track"]
            k = sum(1 for r in v.rows[:i] if r.kind == "track")
            self.play_tracks(tracks, k, source=v.key)

    def on_tab_bar_picked(self, msg: TabBar.Picked):
        self.switch_tab(msg.tab)

    # --- opening things ---

    def open_playlist(self, pl: dict):
        v = View(f"playlist:{pl.get('id')}", pl.get("title") or "playlist")

        def loader():
            tracks, more = self.api.playlist(pl)
            rows = [Row("track", t) for t in tracks]
            if more:
                rows.append(Row("note", text=f"+{more} more not loaded"))
            return rows, None
        self._load(v, loader)
        self.push_view(v)

    def open_user(self, user: dict):
        v = View(f"user:{user.get('id')}", f"tracks by {user.get('username') or '?'}",
                 more=self._more_items)
        self._load(v, lambda: _page_rows(self.api.user_tracks(user["id"])))
        self.push_view(v)

    def search(self, q: str, scope: str = "all"):
        q = q.strip()
        if _SC_URL_RE.match(q):
            self.open_link(q)
            return
        title = f"search: {q}" + (f" · {scope}" if scope != "all" else "")
        v = View("search", title, more=self._more_items)
        self._load(v, lambda: _page_rows(self.api.search(q, scope)))
        self._stacks["search"] = [v]
        self.switch_tab("search")

    def open_link(self, url: str):
        def job():
            j = self.api.resolve(url)
            self._ui(self._opened, j)
        self.say("opening link…", "dim")
        self._bg(job, group="open")

    def _opened(self, j: dict):
        k = kind_of(j)
        if k == "track":
            self.play_tracks([j], 0, source="link")
        elif k == "playlist":
            self.open_playlist(j)
        elif k == "user":
            self.open_user(j)
        else:
            self.say("klangtui can't open that kind of link", "error")

    # --- the queue ---

    def _set_queue(self, tracks: list[dict], mode: str):
        self.queue = list(tracks)
        self.mode = mode
        self._epoch += 1
        self._skipped.clear()
        self._bg(self.store.remember, list(tracks), group="store")
        self._queue_changed()

    def play_tracks(self, tracks: list[dict], index: int, source: str):
        self._set_queue(tracks, "list")
        self.play_index(index, source=source)

    def _save_state(self):
        try:
            self.store.put_json("queue", {
                "ids": [t["id"] for t in self.queue], "index": self.qi,
                "position": round(self.position, 1), "mode": self.mode})
        except Exception:
            pass

    def _restore_queue(self):
        tracks, index, pos, mode = self.store.load_queue()
        if not tracks or index < 0:
            return
        self.queue, self.qi, self.mode = tracks, index, mode
        self.now = tracks[index]
        self.position = self._resume_at = pos
        self.duration = seconds_of(self.now)
        self.paused = True

    # --- playback ---

    def play_index(self, i: int, *, auto: bool = False, start: float = 0.0,
                   source: str | None = None):
        if not 0 <= i < len(self.queue):
            return
        self._close_play(skipped=not auto)
        self._gen += 1
        gen = self._gen
        self.qi = i
        track = self.queue[i]
        self.now = track
        if source:
            self._source = source
        self.position = self._max_pos = start
        self.duration = seconds_of(track)
        self.paused = False
        self.buffering = True
        self.preview = is_preview(track)
        self.wave = None
        self.cover = None
        self._status = "loading…"
        self._resume_at = 0.0
        self._loading = True
        self._at_end = False
        self._refresh_now()
        self.list.refresh()
        self._bg(self._start_track, gen, track, start, auto, group="play")

    def _ensure_mpv(self) -> Mpv:
        with self._mpv_lock:
            if self.mpv is None or not self.mpv.alive:
                if self.mpv is not None:
                    self.mpv.close()
                m = Mpv(lambda kind, value: self.post_message(MpvEvent(kind, value)))
                m.start(self.volume)
                self.mpv = m
            return self.mpv

    def _start_track(self, gen: int, track: dict, start: float, auto: bool):
        """Worker: resolve the stream, hand it to mpv, then fetch the extras."""
        try:
            url, preview, full = self.api.stream(track)
            with self._load_lock:            # never let an older request load last
                if gen != self._gen:
                    return
                mpv = self._ensure_mpv()
                mpv.set_loop(self.repeat == "one")
                self._loaded_gen = gen
                mpv.load(url, start)
        except PlayerError as e:
            self._ui(self._track_failed, gen, track, str(e), False)
            return
        except ApiError as e:
            self._ui(self._track_failed, gen, track, str(e), auto)
            return
        self._ui(self._track_started, gen, full, preview)
        wave = self.api.waveform(full)
        cover = None
        if self.cover_on and art.available():
            url = full.get("artwork_url") or (full.get("user") or {}).get("avatar_url")
            if url:
                try:
                    cover = art.render(self.api.fetch(art.small_url(url)),
                                       PlayerBox.COVER_W, PlayerBox.COVER_H)
                except ApiError:
                    cover = None
        self._ui(self._track_extras, gen, wave, cover)

    def _track_started(self, gen: int, full: dict, preview: bool):
        if gen != self._gen:
            return
        self._status = ""
        self._loading = False
        self._fails = 0
        self.preview = preview
        self.now = full
        if 0 <= self.qi < len(self.queue) and self.queue[self.qi].get("id") == full.get("id"):
            self.queue[self.qi] = full
        self._play_id = self.store.play_started(full, self._source)
        self._save_state()
        self._refresh_now()
        self.list.refresh()
        if preview:
            self.say("only a 30-second preview of this one is on SoundCloud (Go+)", "dim")
        self._top_up_radio()

    def _track_extras(self, gen: int, wave, cover):
        if gen != self._gen:
            return
        self.wave = wave
        self.cover = cover
        self.player.refresh()

    def _track_failed(self, gen: int, track: dict, err: str, auto: bool):
        if gen != self._gen:
            return
        self._status = ""
        self._loading = False
        self.paused = True
        self.buffering = False
        self._loaded_gen = -1
        self.say(f"can't play “{track.get('title') or '?'}”: {err}", "error", 8)
        self._refresh_now()
        if not auto or self._fails >= 5:
            return
        self._fails += 1
        if self.qi + 1 < len(self.queue):
            self.play_index(self.qi + 1, auto=True)
        elif self.autoplay:
            self._at_end = True
            self._extend_radio(play_next=True)

    def _close_play(self, finished: bool = False, skipped: bool = False):
        """Write the outgoing track's play into the history."""
        if self._play_id is None:
            return
        listened = self._max_pos
        done = finished or bool(self.duration and listened >= 0.8 * self.duration)
        try:
            self.store.play_progress(self._play_id, listened, done)
        except Exception:
            pass
        if skipped and listened < 30 and self.now:
            self._skipped.add(self.now.get("id"))
        self._play_id = None

    def on_mpv_event(self, ev: MpvEvent):
        current = self._loaded_gen == self._gen
        if ev.kind == "time":
            if current:
                self.position = ev.value
                self._max_pos = max(self._max_pos, ev.value)
                self.buffering = False
        elif ev.kind == "pause":
            if current:
                self.paused = ev.value
        elif ev.kind == "duration":
            if current:
                self.duration = ev.value
        elif ev.kind == "buffering":
            if current:
                self.buffering = ev.value
        elif ev.kind == "ended":
            if current:
                self._track_ended()
        elif ev.kind == "error":
            if current:
                self._track_failed(self._gen, self.now or {}, str(ev.value), True)
        elif ev.kind == "closed":
            self.mpv = None
            self._loaded_gen = -1
            self.paused = True
            self.say("mpv stopped — space starts it again", "error")
        self._refresh_now()

    def _track_ended(self):
        self._close_play(finished=True)
        self._fails = 0
        if self.qi + 1 < len(self.queue):
            self.play_index(self.qi + 1, auto=True)
        elif self.repeat == "all" and self.queue:
            self.play_index(0, auto=True)
        elif self.autoplay and self.now:
            self._status = "autoplay: finding something similar…"
            self._at_end = True
            self._extend_radio(play_next=True)
        else:
            self.paused = True
            self._loaded_gen = -1
            self.say("end of the queue", "dim")

    def seek_fraction(self, frac: float):
        if self.mpv and self._loaded_gen == self._gen and self.duration:
            try:
                self.mpv.seek(max(0.0, min(1.0, frac)) * self.duration, relative=False)
            except PlayerError as e:
                self.say(str(e), "error")

    # --- the radio ---

    def _radio_seeds(self) -> list[dict]:
        """The last few tracks you let play (skips don't count) — so the radio
        drifts with you instead of circling the first seed forever."""
        seeds = []
        for t in reversed(self.queue[: self.qi + 1]):
            if t.get("id") not in self._skipped:
                seeds.append(t)
            if len(seeds) == 3:
                break
        return seeds or ([self.now] if self.now else [])

    def start_radio(self, seed: dict):
        playing = (self.now is not None and self.now.get("id") == seed.get("id")
                   and self._loaded_gen == self._gen)
        keep = self.queue[: self.qi + 1] if playing else []
        queued = {t.get("id") for t in keep} | {seed.get("id")}
        self.say(f"tuning the radio to “{seed.get('title') or '?'}”…", "dim", 0)
        self._radio_busy = True
        self._bg(self._radio_job, [seed], queued, ("new", seed, playing, self._epoch),
                 group="radio")

    def _extend_radio(self, play_next: bool):
        if play_next:
            self._radio_then_play = True
        if self._radio_busy:
            return
        self._radio_busy = True
        queued = {t.get("id") for t in self.queue}
        self._bg(self._radio_job, self._radio_seeds(), queued, ("more", self._epoch),
                 group="radio")

    def _top_up_radio(self):
        if self.mode == "radio" and len(self.queue) - self.qi - 1 <= 3:
            self._extend_radio(play_next=False)

    def _radio_job(self, seeds, queued, ctx):
        recent = self.store.played_since(3 * 86400)
        heard = self.store.ever_played()
        try:
            batch = build_batch(self.api, seeds, queued=queued, recent=recent,
                                heard=heard, previews=self.previews)
            if 0 < len(batch) < 12:
                # a niche seed: grow a second round from what the first one found
                more = build_batch(self.api, batch[:2], previews=self.previews,
                                   queued=queued | {t["id"] for t in batch},
                                   recent=recent, heard=heard, want=25 - len(batch))
                batch += more
        except ApiError as e:
            self._ui(self._radio_ready, [], ctx, str(e))
            return
        self._ui(self._radio_ready, batch, ctx, None)

    def _radio_ready(self, batch: list[dict], ctx: tuple, err: str | None):
        self._radio_busy = False
        then_play, self._radio_then_play = self._radio_then_play, False
        if then_play:
            self._status = ""
            self._refresh_now()
        if err:
            self.say(f"radio: {err}", "error")
            self._stopped_at_end()
            return
        # never queue a track twice — two top-ups can overlap, rounds can agree
        have = {t.get("id") for t in self.queue} if ctx[0] == "more" else {ctx[1].get("id")}
        fresh = []
        for t in batch:
            if t.get("id") not in have:
                have.add(t.get("id"))
                fresh.append(t)
        batch = fresh
        if ctx[0] == "new":
            _, seed, playing, epoch = ctx
            if not batch:
                self.say("the radio found nothing for this one", "error")
                return
            if playing and epoch == self._epoch:
                self.queue = self.queue[: self.qi + 1] + batch
                self.mode = "radio"
                self._epoch += 1
                self._skipped.clear()
                self._bg(self.store.remember, list(batch), group="store")
                self._queue_changed()
            else:
                self._set_queue([seed] + batch, "radio")
                self.play_index(0, source="radio")
            self.say(f"radio: {len(batch)} tracks around “{seed.get('title') or '?'}”", "ok")
            self.switch_tab("queue")
            return
        _, epoch = ctx
        if epoch != self._epoch:                # the queue was replaced meanwhile
            return
        if not batch:
            if then_play:
                self.say("the radio ran out of new tracks — r on another song", "dim")
                self._stopped_at_end()
            return
        was_end = self.qi + 1 >= len(self.queue)
        self.queue.extend(batch)
        self.mode = "radio"
        self._bg(self.store.remember, list(batch), group="store")
        self._queue_changed()
        if then_play and was_end:
            self.play_index(self.qi + 1, auto=True, source="radio")

    def _stopped_at_end(self):
        """The queue ran out and autoplay found nothing: playback is over
        (unless the track is in fact still playing — n at the end)."""
        if self._at_end:
            self._at_end = False
            self.paused = True
            self._loaded_gen = -1
            self._refresh_now()

    # --- the player box ---

    def player_hint(self) -> Text:
        if self._status:
            return Text(self._status, DIM)
        if self.buffering and not self.paused and self._loaded_gen == self._gen:
            return Text("buffering…", DIM)
        if self.now is not None and self._loaded_gen != self._gen and self._resume_at:
            return Text(f"space resumes from {fmt_time(self._resume_at)}", DIM)
        if self.repeat == "one":
            return Text("repeating this track", DIM)
        if self.qi + 1 < len(self.queue):
            nxt = self.queue[self.qi + 1]
            t = Text("next  ", DIM)
            t.append(clean(nxt.get("title") or "?"))
            t.append(f" — {clean(artist_of(nxt))}", DIM)
            return t
        if self._radio_busy:
            return Text("next  the radio is picking…", DIM)
        if self.autoplay and self.now is not None:
            return Text("next  something similar (autoplay)", DIM)
        return Text("end of the queue", DIM)

    def _refresh_now(self):
        bits = [f"vol {self.volume}"]
        if self.repeat != "off":
            bits.append(f"repeat {self.repeat}")
        if self.mode == "radio":
            bits.append("radio")
        if self.queue:
            bits.append(f"{self.qi + 1}/{len(self.queue)}")
        self.player.border_subtitle = " · ".join(bits)
        self.player.refresh()

    def _refresh_account(self):
        acc = self.query_one("#account", Static)
        if self.user:
            acc.update(Text("@" + (self.user.get("permalink") or self.user.get("username")
                                   or "you"), DIM))
        else:
            acc.update(Text.assemble(("guest", DIM), ("  :login", DIM)))

    # --- controls ---

    def _is_loaded(self) -> bool:
        return self.mpv is not None and self._loaded_gen == self._gen and self.mpv.alive

    def action_toggle_pause(self):
        if self.now is None:
            self.say("nothing to play — / searches, enter plays", "dim")
            return
        if self._loading:
            return
        if not self._is_loaded():
            if 0 <= self.qi < len(self.queue):
                self.play_index(self.qi, start=self._resume_at or self.position, source="resume")
            return
        try:
            self.mpv.set_pause(not self.paused)
        except PlayerError as e:
            self.say(str(e), "error")

    def action_next(self):
        if self.qi + 1 < len(self.queue):
            self.play_index(self.qi + 1)
        elif self.now is not None and self.autoplay:
            self._close_play(skipped=True)
            self.say("asking the radio for something similar…", "dim")
            self._extend_radio(play_next=True)
        else:
            self.say("end of the queue", "dim")

    def action_prev(self):
        if self.position > 5 and self._is_loaded():
            self.action_seek(-self.position)
        elif self.qi > 0:
            self.play_index(self.qi - 1)
        else:
            self.say("already at the start of the queue", "dim")

    def action_seek(self, delta: float):
        if not self._is_loaded():
            return
        try:
            self.mpv.seek(delta, relative=True)
        except PlayerError as e:
            self.say(str(e), "error")

    def action_volume(self, delta: int):
        self.set_volume(self.volume + delta)

    def set_volume(self, v: int):
        v = max(0, min(100, int(v)))
        self.volume = v
        self.store.put("volume", str(v))
        if self.mpv and self.mpv.alive:
            try:
                self.mpv.set_volume(v)
            except PlayerError:
                pass
        self._refresh_now()

    def action_repeat(self):
        modes = ("off", "all", "one")
        self.set_repeat(modes[(modes.index(self.repeat) + 1) % 3])

    def set_repeat(self, mode: str):
        self.repeat = mode
        self.store.put("repeat", mode)
        if self.mpv and self.mpv.alive:
            try:
                self.mpv.set_loop(mode == "one")
            except PlayerError:
                pass
        self.say(f"repeat {mode}", "ok")
        self._refresh_now()

    def action_shuffle(self):
        rest = self.queue[self.qi + 1:]
        if len(rest) < 2:
            self.say("nothing to shuffle", "dim")
            return
        random.shuffle(rest)
        self.queue[self.qi + 1:] = rest
        self._queue_changed()
        self.say(f"shuffled {len(rest)} upcoming tracks", "ok")

    def action_radio(self):
        seed = self._selected_track() or self.now
        if seed is None:
            self.say("pick a track first — the radio grows from it", "dim")
            return
        self.start_radio(seed)

    def action_like(self, playing: bool):
        track = self.now if playing else (self._selected_track() or self.now)
        if track is None:
            self.say("pick a track to like", "dim")
            return
        self.like(track, track.get("id") not in self.liked)

    def like(self, track: dict, on: bool):
        if not self.user:
            self.say("sign in to like tracks — :login", "error")
            return

        def job():
            uid, tid = self.user["id"], track["id"]
            status = self.api.set_like(uid, tid, on)
            if status == 403:
                status = browser_write(
                    self.session, self.api.like_url(uid, tid), "PUT" if on else "DELETE",
                    self.api.token or "", lambda s: self._ui(self.say, s, "dim", 0))
            if status not in (200, 201, 204):
                raise ApiError(f"SoundCloud refused the like (HTTP {status})")
            self._ui(self._like_done, track, on)
        self._bg(job, group="like")

    def _like_done(self, track: dict, on: bool):
        (self.liked.add if on else self.liked.discard)(track["id"])
        self.store.set_liked(track["id"], on)
        self.say(("♥ liked  " if on else "like removed  ") + (track.get("title") or "?"), "ok")
        self.list.refresh()
        self.player.refresh()

    def action_enqueue(self, play_next: bool):
        track = self._selected_track()
        if track is None:
            self.say("select a track to queue it", "dim")
            return
        if self.qi < 0 or self.now is None:
            self.play_tracks([track], 0, source="queue")
            return
        if play_next:
            self.queue.insert(self.qi + 1, track)
        else:
            self.queue.append(track)
        self._bg(self.store.remember, [track], group="store")
        self._queue_changed()
        self.say(("plays next: " if play_next else "queued: ") + (track.get("title") or "?"),
                 "ok")

    def action_remove(self):
        v, row = self._selected()
        if not v or v.key != "queue" or not row or row.kind != "track":
            return
        i = v.cursor
        if i == self.qi:
            self.say("that one's playing — n skips it", "dim")
            return
        del self.queue[i]
        if i < self.qi:
            self.qi -= 1
        self._queue_changed()

    def action_info(self):
        _, row = self._selected()
        item = row.item if row and row.item else self.now
        if item:
            title = (item.get("title") or item.get("username") or "info")
            self.push_screen(TextScreen(title, info_text(item)))

    def action_open_web(self):
        _, row = self._selected()
        item = row.item if row and row.item else self.now
        if item and item.get("permalink_url"):
            webbrowser.open(item["permalink_url"])
            self.say("opened in your browser", "dim")

    def action_help(self):
        self.push_screen(TextScreen("help", help_text()))

    # --- account ---

    def _me_job(self):
        try:
            me = self.api.me()
        except AuthError as e:
            self._ui(self._signed_out, str(e))
            return
        self._ui(self._set_user, me)
        self._ui(self._set_liked_ids, self.api.liked_ids(me["id"]))

    def _set_user(self, me: dict):
        self.user = me
        self._refresh_account()
        for tab in ("likes", "library"):       # drop the "sign in" placeholders
            self._stacks[tab] = []
            if self.tab == tab:
                self.switch_tab(tab)

    def _set_liked_ids(self, ids: set[int]):
        self.liked = ids
        self._bg(self.store.set_liked_ids, set(ids), group="store")
        self.list.refresh()
        self.player.refresh()

    def _signed_out(self, msg: str):
        self.session.clear()
        self.api.token = None
        self.user = None
        self._refresh_account()
        for tab in ("likes", "library"):
            self._stacks[tab] = []
        self.say(msg, "error", 8)

    def login(self):
        if self.user:
            self.say(f"already signed in as {self.user.get('username')} — :logout first", "dim")
            return
        if self._login_cancel is not None:
            self.say("the sign-in window is already open", "dim")
            return
        cancel = threading.Event()
        self._login_cancel = cancel

        def job():
            try:
                tok = login_window(self.session, lambda s: self._ui(self.say, s, "dim", 0),
                                   cancel)
                self.api.token = tok
                me = self.api.me()
            except ApiError as e:
                self._ui(self._login_done, None, str(e))
                return
            self._ui(self._login_done, me, None)
            self._ui(self._set_liked_ids, self.api.liked_ids(me["id"]))
        self._bg(job, group="login")

    def _login_done(self, me: dict | None, err: str | None):
        self._login_cancel = None
        if err:
            self.say(err, "error", 8)
            return
        self._set_user(me)
        self.say(f"signed in as {me.get('username') or 'you'}", "ok")

    def logout(self):
        if not self.user and not self.api.token:
            self.say("you're not signed in", "dim")
            return
        self.session.clear()
        self.api.token = None
        self.user = None
        self.liked = set()
        self._bg(self.store.set_liked_ids, set(), group="store")
        for tab in ("likes", "library"):
            self._stacks[tab] = []
        if self.tab in ("likes", "library"):
            self.switch_tab(self.tab)
        self._refresh_account()
        self.say("signed out — the login is gone", "ok")

    # --- the prompt (/ search · : commands) ---

    def action_prompt(self, mode: str):
        if isinstance(self.screen, ModalScreen):
            return
        self._prompt_mode = mode
        prefix = self.query_one("#prefix", Static)
        prefix.update(Text(mode, ACCENT))
        prefix.display = True
        self.status.display = False
        self.prompt.display = True
        self.prompt.placeholder = ("search soundcloud — or paste a link" if mode == "/"
                                   else "command   (tab completes · ? lists them all)")
        if self.prompt.value:
            self._own_edit = True
            self.prompt.value = ""
        self._hist_i = len(self._hist[mode])
        self._draft = ""
        self.prompt.focus()
        self._refresh_suggest("")

    def _close_prompt(self):
        self._prompt_mode = ""
        self.query_one("#prefix", Static).display = False
        self.prompt.display = False
        self.status.display = True
        self.query_one("#suggest", Static).display = False
        self._sugg, self._sugg_i = [], -1
        self.list.focus()

    def on_input_submitted(self, event: Input.Submitted):
        mode, text = self._prompt_mode, event.value.strip()
        self._close_prompt()
        if not text or not mode:
            return
        hist = self._hist[mode]
        if not hist or hist[-1] != text:
            hist.append(text)
            del hist[:-100]
            self.store.put_json("search_history" if mode == "/" else "command_history", hist)
        if mode == "/":
            self.search(text)
        else:
            self.run_command(text)

    def on_input_changed(self, event: Input.Changed):
        if self._own_edit:                  # our own fill (history / tab) — keep the menu
            self._own_edit = False
            return
        self._sugg_i = -1
        self._refresh_suggest(event.value)

    def _refresh_suggest(self, value: str):
        self._sugg = suggestions(value) if self._prompt_mode == ":" else []
        self._sugg_i = -1
        self._draw_suggest()

    def _draw_suggest(self):
        box = self.query_one("#suggest", Static)
        if not self._sugg:
            box.display = False
            return
        win = 8
        start = max(0, min(self._sugg_i - win + 1, len(self._sugg) - win)) \
            if self._sugg_i >= win else 0
        t = Text(no_wrap=True)
        for k, (_, label, desc) in enumerate(self._sugg[start:start + win], start):
            if k > start:
                t.append("\n")
            line = Text(no_wrap=True)
            line.append(f"{label:<34}", YELLOW)
            line.append(desc, DIM)
            if k == self._sugg_i:
                line.stylize(CURSOR)
            t.append_text(line)
        box.update(t)
        box.border_title = "commands"
        box.border_subtitle = f"{len(self._sugg)} matches" if len(self._sugg) > win else ""
        box.display = True

    def _complete(self):
        if not self._sugg:
            return
        self._sugg_i = (self._sugg_i + 1) % len(self._sugg)
        self._fill(self._sugg[self._sugg_i][0])
        if len(self._sugg) == 1:            # the only match: step into its arguments
            self._refresh_suggest(self.prompt.value)
        else:
            self._draw_suggest()

    def _fill(self, text: str):
        if text != self.prompt.value:
            self._own_edit = True
            self.prompt.value = text
        self.prompt.cursor_position = len(text)

    def action_prompt_move(self, d: int):
        if self._sugg:
            self._sugg_i = (self._sugg_i + d) % len(self._sugg) if self._sugg_i >= 0 \
                else (0 if d > 0 else len(self._sugg) - 1)
            self._fill(self._sugg[self._sugg_i][0])
            self._draw_suggest()
            return
        hist = self._hist.get(self._prompt_mode) or []
        if not hist:
            return
        if self._hist_i >= len(hist):
            self._draft = self.prompt.value
        self._hist_i = max(0, min(len(hist), self._hist_i + d))
        self._fill(self._draft if self._hist_i == len(hist) else hist[self._hist_i])

    # --- commands ---

    def run_command(self, line: str):
        cmd, _, arg = line.strip().lstrip(":/").partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        onoff = {"on": True, "yes": True, "1": True, "off": False, "no": False, "0": False}
        if cmd in ("search", "s", "find"):
            first, _, rest = arg.partition(" ")
            scopes = {"tracks": "tracks", "sets": "playlists", "playlists": "playlists",
                      "albums": "playlists", "people": "people", "users": "people",
                      "artists": "people"}
            if first.lower() in scopes and rest.strip():
                self.search(rest, scopes[first.lower()])
            elif arg:
                self.search(arg)
            else:
                self.action_prompt("/")
        elif cmd == "open":
            if _SC_URL_RE.match(arg):
                self.open_link(arg)
            else:
                self.say("usage: :open <soundcloud.com link>", "error")
        elif cmd == "radio":
            if self.now:
                self.start_radio(self.now)
            else:
                self.action_radio()
        elif cmd in ("like", "unlike"):
            if self.now:
                self.like(self.now, cmd == "like")
            else:
                self.say("nothing is playing", "dim")
        elif cmd in ("volume", "vol", "v"):
            try:
                self.set_volume(int(arg.rstrip("%")))
            except ValueError:
                self.say(f"volume {self.volume} · usage: :volume <0-100>", "dim")
        elif cmd == "seek":
            m = re.fullmatch(r"([+-]?)(?:(\d+):)?(\d+(?:\.\d+)?)", arg)
            if not m or not self._is_loaded():
                self.say("usage: :seek 1:30 · :seek +15 · :seek -15", "dim")
                return
            sign, mins, secs = m.groups()
            value = (int(mins) * 60 if mins else 0) + float(secs)
            try:
                if sign:
                    self.mpv.seek(-value if sign == "-" else value, relative=True)
                else:
                    self.mpv.seek(value, relative=False)
            except PlayerError as e:
                self.say(str(e), "error")
        elif cmd == "repeat":
            if arg in ("off", "all", "one"):
                self.set_repeat(arg)
            else:
                self.action_repeat()
        elif cmd in ("autoplay", "previews", "cover"):
            cur = getattr(self, "cover_on" if cmd == "cover" else cmd)
            val = onoff.get(arg.lower(), not cur) if arg else not cur
            setattr(self, "cover_on" if cmd == "cover" else cmd, val)
            self.store.put(cmd, "1" if val else "0")
            self.say(f"{cmd} {'on' if val else 'off'}", "ok")
            if cmd == "cover" and val and not art.available():
                self.say("cover art needs Pillow: pip install pillow", "error")
            self.player.refresh()
        elif cmd == "shuffle":
            self.action_shuffle()
        elif cmd == "clear":
            keep = [self.now] if self.now is not None and self._is_loaded() else []
            self.qi = 0 if keep else -1
            if not keep:
                self.now = None
            self._set_queue(keep, "list")
            self.say("queue cleared", "ok")
        elif cmd == "history":
            if arg == "clear":
                self.store.clear_history()
                if self.tab == "history":
                    self.switch_tab("history")
                self.say("listening history cleared", "ok")
            else:
                self.switch_tab("history")
        elif cmd in ("next", "n", "skip"):
            self.action_next()
        elif cmd in ("prev", "previous"):
            self.action_prev()
        elif cmd in ("pause", "play", "resume", "p"):
            self.action_toggle_pause()
        elif cmd == "login":
            self.login()
        elif cmd == "logout":
            self.logout()
        elif cmd in ("help", "h", "?", "keys"):
            self.action_help()
        elif cmd in ("quit", "q", "exit"):
            self.action_quit()
        else:
            self.say(f"unknown command “{cmd}” — ? lists them", "error")

    # --- leaving ---

    def shutdown(self):
        """Save where you were and stop mpv. Safe to call more than once."""
        if self._shut:
            return
        self._shut = True
        self._close_play()
        self._save_state()
        if self._login_cancel is not None:
            self._login_cancel.set()
        if self.mpv is not None:
            self.mpv.close()
            self.mpv = None
        self.api.close()

    def action_quit(self):
        self.shutdown()
        self.exit()


# --- command line ---------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        prog="klangtui",
        description="SoundCloud in your terminal — search, play, radio, likes, history")
    p.add_argument("-q", "--query", metavar="TEXT", help="search for TEXT right away")
    p.add_argument("--clear-data", action="store_true",
                   help="delete ~/.klangtui (settings, history and the login) and exit")
    p.add_argument("--version", action="version", version=f"klangtui {__version__}")
    args = p.parse_args()

    if args.clear_data:
        if DATA_DIR.exists():
            shutil.rmtree(DATA_DIR, ignore_errors=True)
            print("cleared ~/.klangtui — settings, history and login removed")
        else:
            print("nothing to clear")
        sys.exit(0)

    app = Klangtui(query=args.query)
    atexit.register(app.shutdown)           # mpv never outlives klangtui
    try:
        app.run()
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
