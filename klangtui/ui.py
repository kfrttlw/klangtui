"""What klangtui draws: the tab bar, the now-playing box, the list, the help.

Everything here reads the app's state (`self.app`) and paints it; the
behaviour — playback, the queue, the radio — lives in app.py.
"""

from __future__ import annotations

import bisect
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.geometry import Size
from textual.message import Message
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import Input, Static

from . import art
from .api import Page, artist_of, is_blocked, is_preview, kind_of, seconds_of

if TYPE_CHECKING:
    from .app import Klangtui

TABS = ("home", "search", "likes", "library", "queue", "history")

_SC_URL_RE = re.compile(r"https?://(?:www\.|m\.|on\.)?soundcloud\.com/\S+")

# --- the terminal's own colours ----------------------------------------------

TERMINAL = Theme(
    name="terminal", ansi=True, dark=True,
    primary="ansi_yellow", secondary="ansi_cyan", accent="ansi_yellow",
    warning="ansi_yellow", error="ansi_red", success="ansi_green",
    foreground="ansi_default", background="ansi_default", surface="ansi_default",
    panel="ansi_default", boost="ansi_default",
    variables={
        "ansi-background": "ansi_default",
        "ansi-foreground": "ansi_default",
        "border": "ansi_default",
        "border-blurred": "ansi_bright_black",
        "input-cursor-background": "ansi_default",
        "input-cursor-foreground": "ansi_default",
        "input-cursor-text-style": "reverse",
        "input-selection-background": "ansi_bright_black",
        "input-selection-foreground": "ansi_default",
        "screen-selection-background": "ansi_bright_black",
        "screen-selection-foreground": "ansi_default",
    },
)

ACCENT = Style(color="yellow", bold=True)
YELLOW = Style(color="yellow")
DIM = Style(dim=True)
BOLD = Style(bold=True)
RED = Style(color="red")
GREEN = Style(color="green")
CYAN = Style(color="cyan")
CURSOR = Style(reverse=True)
CURSOR_BLUR = Style(bold=True, underline=True)

WAVE = "▁▂▃▄▅▆▇█"


def fmt_time(s: float | None) -> str:
    if s is None or s != s or s < 0:
        return "-:--"
    s = int(s)
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def fmt_count(n) -> str:
    if not isinstance(n, (int, float)):
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def fmt_ago(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "now"
    for size, unit in ((604800, "w"), (86400, "d"), (3600, "h"), (60, "m")):
        if d >= size:
            return f"{int(d // size)}{unit} ago"
    return ""


# Emoji variation selectors make terminals disagree on a glyph's width (1 cell
# or 2), which shifts the rest of the row — drop them, keep the plain glyph.
_INVISIBLE = dict.fromkeys(map(ord, "\ufe0e\ufe0f\u200d"))


def clean(s: str) -> str:
    return s.translate(_INVISIBLE)


def _cell(text: str | Text, width: int, style: Style | None = None,
          right: bool = False) -> Text:
    """text cut (…) or padded to exactly `width` cells — CJK-safe."""
    t = text.copy() if isinstance(text, Text) else Text(clean(text), style=style or "")
    t.no_wrap = True
    if t.cell_len > width:
        t.truncate(width, overflow="ellipsis")
    pad = width - t.cell_len
    if pad > 0:
        if right:
            t = Text(" " * pad) + t
        else:
            t.append(" " * pad)
    return t


# --- what the list box shows ---------------------------------------------------

@dataclass
class Row:
    kind: str                    # track · playlist · user · head · note · more
    item: dict | None = None
    text: str = ""
    when: float = 0              # history: last played
    count: int = 0               # history: times played

    @property
    def selectable(self) -> bool:
        return self.kind in ("track", "playlist", "user", "more")


@dataclass
class View:
    key: str                     # "home", "search", "likes", "playlist:123" …
    title: str
    rows: list[Row] = field(default_factory=list)
    cursor: int = 0
    loading: bool = False
    loader: Callable | None = None        # () → (rows, next_href); ctrl+r reruns it
    more: Callable | None = None          # next_href → (rows, next_href)
    next_href: str | None = None
    fetching_more: bool = False


def _note(text: str) -> list[Row]:
    return [Row("note", text=text)]


def _item_rows(items: list[dict]) -> list[Row]:
    return [Row(kind_of(it), it) for it in items if kind_of(it) != "?"]


def _page_rows(page: Page) -> tuple[list[Row], str | None]:
    return _item_rows(page.items), page.next_href


def row_text(a: Klangtui, v: View, i: int, w: int) -> Text:
    """One row of the list box, cut to exactly w cells."""
    row = v.rows[i]
    if row.kind == "head":
        t = Text(no_wrap=True)
        t.append("── ", DIM)
        t.append(row.text, ACCENT)
        t.append(" " + "─" * max(0, w - len(row.text) - 4), DIM)
        return t
    if row.kind in ("note", "more"):
        return _cell(Text("  " + row.text, DIM), w)
    item = row.item or {}
    if v.key == "queue":
        playing = i == a.qi
    else:
        playing = a.now is not None and item.get("id") == a.now.get("id") \
            and row.kind == "track"
    t = Text(no_wrap=True)
    if playing:
        t.append("▶ ", ACCENT)
    else:
        t.append("  ")
    rest = w - 2
    if rest < 12:
        return t + _cell(item.get("title") or item.get("username") or "?", rest)
    wide = rest >= 70
    right_w = 8 if wide else 0
    dur_w = 7
    main = rest - dur_w - right_w
    title_w = max(8, int(main * 0.6))
    artist_w = max(0, main - title_w - 2)
    kind = row.kind
    if kind == "track":
        tags = Text()
        if item.get("id") in a.liked:
            tags.append(" ♥", RED)
        if is_preview(item):
            tags.append(" 30s", YELLOW)
        elif is_blocked(item):
            tags.append(" geo", RED)
        title = _cell(item.get("title") or "?", max(1, title_w - tags.cell_len),
                      BOLD if playing else None)
        t.append_text(title + tags)
        t.append("  ")
        t.append_text(_cell(artist_of(item), artist_w))
        t.append_text(_cell(fmt_time(seconds_of(item)), dur_w, DIM, right=True))
        if wide:
            extra = (fmt_ago(row.when) if v.key == "history"
                     else fmt_count(item.get("playback_count")))
            t.append_text(_cell(extra, right_w, DIM, right=True))
    elif kind == "playlist":
        tag = "album " if item.get("is_album") else "set "
        title = Text.assemble((tag, CYAN), clean(item.get("title") or "?"))
        t.append_text(_cell(title, title_w))
        t.append("  ")
        owner = artist_of(item) if item.get("user") else "soundcloud"
        t.append_text(_cell(owner, artist_w))
        n = item.get("track_count") or len(item.get("tracks") or [])
        t.append_text(_cell(f"{n} trk" if n else "", dur_w, DIM, right=True))
        if wide:
            t.append(" " * right_w)
    elif kind == "user":
        title = Text.assemble(("artist ", CYAN), clean(item.get("username") or "?"))
        t.append_text(_cell(title, title_w))
        t.append("  ")
        place = ", ".join(x for x in (item.get("city"), item.get("country_code")) if x)
        t.append_text(_cell(place, artist_w, DIM))
        t.append_text(_cell(fmt_count(item.get("followers_count")), dur_w, DIM,
                            right=True))
        if wide:
            t.append_text(_cell(" fol", right_w, DIM))
    return t


# --- widgets ------------------------------------------------------------------

class TabBar(Static):
    """` [home]  search  likes …` — click a name, or press 1–6."""

    class Picked(Message):
        def __init__(self, tab: str):
            super().__init__()
            self.tab = tab

    def __init__(self, **kw):
        super().__init__(**kw)
        self.active = TABS[0]
        self._hits: list[tuple[int, int, str]] = []

    def on_mount(self):
        self.set_active(self.active)

    def set_active(self, tab: str):
        self.active = tab
        t = Text(no_wrap=True)
        self._hits = []
        for name in TABS:
            if t.cell_len:
                t.append("  ")
            label = f"[{name}]" if name == tab else name
            self._hits.append((t.cell_len, t.cell_len + len(label), name))
            t.append(label, ACCENT if name == tab else Style())
        self.update(t)

    def on_click(self, event: events.Click):
        off = event.get_content_offset(self)
        if off is None:
            return
        for a, b, name in self._hits:
            if a <= off.x < b:
                self.post_message(self.Picked(name))


class PlayerBox(Widget):
    """The now-playing box: cover · title · artist · waveform scrubber · next up.
    Click the waveform to seek, scroll over the box for volume."""

    COVER_W, COVER_H = 8, 4

    def __init__(self, **kw):
        super().__init__(**kw)
        self._scrub = (-1, 0, 0)               # (row, first column, width)
        self._wave_cache: tuple = (None, 0, "")

    def _wave(self, wave: list[float] | None, n: int) -> str:
        key = (id(wave), n)
        if self._wave_cache[:2] == key:
            return self._wave_cache[2]
        m = len(wave)
        levels = []
        for i in range(n):
            a, b = i * m // n, max(i * m // n + 1, (i + 1) * m // n)
            levels.append(sum(wave[a:b]) / (b - a))
        # stretch between the quiet end and the loudest bucket, like the site
        # does — a loud master would otherwise be one flat block
        ordered = sorted(levels)
        lo, hi = ordered[len(ordered) // 20], ordered[-1]
        span = (hi - lo) or 1.0
        s = "".join(WAVE[1 + min(6, max(0, int((v - lo) / span * 7)))] for v in levels)
        self._wave_cache = (*key, s)
        return s

    def _cover_cols(self, a: Klangtui) -> int:
        """Columns the cover takes (with its gap), 0 when there's none to show."""
        if a.now is None or not a.cover_on or not art.available() or self.size.width < 48:
            return 0
        return self.COVER_W + 2

    def render(self) -> Text:
        a: Klangtui = self.app            # type: ignore[assignment]
        w = max(10, self.size.width)
        cw = self._cover_cols(a)
        info = self._info(a, w - cw)
        cover = a.cover.split("\n") if (cw and a.cover is not None) else None
        out = Text(no_wrap=True)
        for r in range(self.COVER_H):
            if r:
                out.append("\n")
            if cw:
                out.append_text(cover[r] if cover and r < len(cover)
                                else Text(" " * self.COVER_W))
                out.append("  ")
            out.append_text(info[r])
        return out

    def _info(self, a: Klangtui, w: int) -> list[Text]:
        self._scrub = (-1, 0, 0)
        t = a.now
        if t is None:
            return [_cell(Text("nothing playing", DIM), w),
                    _cell(Text("/ search · enter plays · ? help", DIM), w),
                    Text(""), Text("")]
        # 1 · state, title, tags
        tags = Text()
        if t.get("id") in a.liked:
            tags.append("  ♥", RED)
        if a.preview:
            tags.append("  30s preview", YELLOW)
        head = Text()
        head.append("‖ " if a.paused else "▶ ", ACCENT)
        head.append(clean(t.get("title") or "?"), BOLD)
        line1 = _cell(head, max(1, w - tags.cell_len)) + tags
        # 2 · artist · genre
        sub = Text(clean(artist_of(t)))
        if t.get("genre"):
            sub.append(f"  ·  {t['genre']}", DIM)
        line2 = _cell(sub, w)
        # 3 · the waveform scrubber
        dur = a.duration or seconds_of(t)
        left, right = f"{fmt_time(a.position)} ", f" {fmt_time(dur)}"
        n = max(4, w - len(left) - len(right))
        filled = max(0, min(n, int(n * a.position / dur))) if dur else 0
        line3 = Text(left, DIM)
        if a.wave:
            g = self._wave(a.wave, n)
            line3.append(g[:filled], YELLOW)
            line3.append(g[filled:], DIM)
        else:
            line3.append("━" * filled, YELLOW)
            line3.append("─" * (n - filled), DIM)
        line3.append(right, DIM)
        self._scrub = (2, self._cover_cols(a) + len(left), n)
        # 4 · what's happening / what's next
        return [line1, line2, line3, _cell(a.player_hint(), w)]

    def on_click(self, event: events.Click):
        off = event.get_content_offset(self)
        if off is None:
            return
        row, x0, n = self._scrub
        if off.y == row and x0 <= off.x < x0 + n:
            self.app.seek_fraction((off.x - x0) / n)      # type: ignore[attr-defined]
        else:
            self.app.switch_tab("queue")                  # type: ignore[attr-defined]

    def on_mouse_scroll_up(self, event: events.MouseScrollUp):
        event.stop()
        self.app.action_volume(5)                         # type: ignore[attr-defined]

    def on_mouse_scroll_down(self, event: events.MouseScrollDown):
        event.stop()
        self.app.action_volume(-5)                        # type: ignore[attr-defined]


class ListBox(ScrollView, can_focus=True, inherit_bindings=False):
    """A virtual list — only the rows on screen are drawn, so a 1000-track
    playlist costs the same as a 10-track one."""

    BINDINGS = [
        Binding("down,j", "move(1)", show=False),
        Binding("up,k", "move(-1)", show=False),
        Binding("home,g", "edge(-1)", show=False),
        Binding("end,G", "edge(1)", show=False),
        Binding("pagedown,ctrl+d", "page(1)", show=False),
        Binding("pageup,ctrl+u", "page(-1)", show=False),
        Binding("enter", "activate", show=False),
    ]

    class Activated(Message):
        def __init__(self, view: View, index: int):
            super().__init__()
            self.view = view
            self.index = index

    class NearEnd(Message):
        def __init__(self, view: View):
            super().__init__()
            self.view = view

    def __init__(self, **kw):
        super().__init__(**kw)
        self.view: View | None = None
        self._sel: list[int] = []          # indices of selectable rows

    # --- showing a view ---

    def show(self, view: View, *, center: bool = False):
        self.view = view
        self._sel = [i for i, r in enumerate(view.rows) if r.selectable]
        if view.cursor not in self._sel:
            view.cursor = self._sel[0] if self._sel else -1
        self.border_title = view.title + ("  · loading…" if view.loading else "")
        self._sync_size()
        self._keep_visible(center=center)
        self._subtitle()
        self.refresh()

    def _sync_size(self):
        n = len(self.view.rows) if self.view else 0
        self.virtual_size = Size(self.scrollable_content_region.width, n)

    def on_resize(self, event: events.Resize):
        self._sync_size()
        self._keep_visible()

    def _subtitle(self):
        v = self.view
        items = [i for i in self._sel if v and v.rows[i].kind != "more"]
        if not v or not items or v.cursor < 0:
            self.border_subtitle = ""
            return
        k = bisect.bisect_left(items, v.cursor) + 1
        self.border_subtitle = f"{min(k, len(items))} of {len(items)}"

    def _keep_visible(self, center: bool = False):
        v = self.view
        h = self.scrollable_content_region.height
        if not v or v.cursor < 0 or h <= 0:
            return
        top = int(self.scroll_y)
        if center:
            target = max(0, v.cursor - h // 3)
        elif v.cursor < top:
            target = v.cursor
        elif v.cursor >= top + h:
            target = v.cursor - h + 1
        else:
            return
        # keep a section's heading in view with its first item
        if target == v.cursor and v.cursor > 0 and v.rows[v.cursor - 1].kind == "head":
            target -= 1
        self.scroll_to(y=target, animate=False, immediate=True)

    def set_cursor(self, i: int):
        v = self.view
        if not v or i not in self._sel:
            return
        v.cursor = i
        self._keep_visible()
        self._subtitle()
        self.refresh()
        if v.next_href and i >= len(v.rows) - 8:
            self.post_message(self.NearEnd(v))

    # --- keys ---

    def action_move(self, d: int):
        if not self._sel or not self.view:
            return
        k = bisect.bisect_left(self._sel, self.view.cursor) + d
        self.set_cursor(self._sel[max(0, min(len(self._sel) - 1, k))])

    def action_edge(self, d: int):
        if self._sel:
            self.set_cursor(self._sel[-1] if d > 0 else self._sel[0])

    def action_page(self, d: int):
        if not self._sel or not self.view:
            return
        h = max(1, self.scrollable_content_region.height - 1)
        k = bisect.bisect_left(self._sel, self.view.cursor + d * h)
        self.set_cursor(self._sel[max(0, min(len(self._sel) - 1, k))])

    def action_activate(self):
        if self.view and self.view.cursor >= 0:
            self.post_message(self.Activated(self.view, self.view.cursor))

    # --- mouse ---

    def on_click(self, event: events.Click):
        off = event.get_content_offset(self)
        if off is None or not self.view:
            return
        i = off.y + int(self.scroll_y)
        if i not in self._sel:
            return
        again = i == self.view.cursor
        self.set_cursor(i)
        if again or event.chain >= 2:
            self.action_activate()

    # --- drawing ---

    def on_focus(self):
        self.refresh()

    def on_blur(self):
        self.refresh()

    def render_line(self, y: int) -> Strip:
        w = self.scrollable_content_region.width
        v = self.view
        i = y + int(self.scroll_y)
        if v is None or not 0 <= i < len(v.rows):
            return Strip.blank(w)
        text = row_text(self.app, v, i, w)                # type: ignore[arg-type]
        strip = Strip(list(text.render(self.app.console)), text.cell_len)
        strip = strip.extend_cell_length(w).crop(0, w)
        if i == v.cursor:
            strip = strip.apply_style(CURSOR if self.has_focus else CURSOR_BLUR)
        return strip


class PromptInput(Input):
    """The footer prompt — ↑/↓ walk the history or the command menu."""

    BINDINGS = [
        Binding("up", "app.prompt_move(-1)", show=False),
        Binding("down", "app.prompt_move(1)", show=False),
    ]


class TextScreen(ModalScreen):
    """A boxed page over the app (help, track info) — esc / q closes it."""

    BINDINGS = [Binding("escape,q,question_mark,i", "dismiss", show=False)]

    DEFAULT_CSS = """
    TextScreen {
        align: center middle;
    }
    TextScreen > VerticalScroll {
        width: 84;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        padding: 0 1;
        border: solid ansi_default;
        border-title-color: ansi_yellow;
        border-title-style: bold;
        border-subtitle-color: ansi_bright_black;
        background: ansi_default;
        scrollbar-size-vertical: 1;
        scrollbar-color: ansi_bright_black;
        scrollbar-background: ansi_default;
    }
    """

    def __init__(self, title: str, body: Text):
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        box = VerticalScroll(Static(self._body))
        box.border_title = self._title
        box.border_subtitle = "esc closes"
        yield box


class MpvEvent(Message):
    """Something mpv reported (posted from its reader thread)."""

    def __init__(self, kind: str, value):
        super().__init__()
        self.kind = kind
        self.value = value


# --- help & commands -----------------------------------------------------------

KEYS: list[tuple[str, str]] = [
    ("1-6  tab  [ ]", "tabs: home · search · likes · library · queue · history"),
    ("j k  ↑ ↓", "move   (g / G top / bottom · ctrl+d / ctrl+u page)"),
    ("enter", "play a track · open a playlist or an artist"),
    ("esc", "back · close the prompt · cancel sign-in"),
    ("/", "search — or paste a soundcloud.com link"),
    (":", "command line (tab completes)"),
    ("space", "play / pause"),
    ("n  p", "next / previous track"),
    ("←  →", "seek 5 s   (shift: 30 s)"),
    ("+  -", "volume (or scroll over the player)"),
    ("r", "radio from the selected track"),
    ("l  L", "like the selected / the playing track"),
    ("a  A", "add to the queue · play next"),
    ("d", "remove from the queue (queue tab)"),
    ("s", "shuffle what's coming up"),
    ("m", "repeat: off → all → one"),
    ("i  o", "info · open on soundcloud.com"),
    ("ctrl+r", "reload this page"),
    ("q", "quit"),
]

COMMANDS: list[tuple[str, str, str]] = [
    ("search", "[tracks|sets|people] <text>", "search SoundCloud"),
    ("open", "<url>", "open a soundcloud.com link"),
    ("radio", "", "radio from the playing track"),
    ("like", "", "like the playing track"),
    ("unlike", "", "remove that like"),
    ("volume", "<0-100>", "set the volume"),
    ("seek", "<m:ss|±s>", "jump within the track"),
    ("repeat", "<off|all|one>", "repeat mode"),
    ("autoplay", "<on|off>", "keep playing similar music when the queue ends"),
    ("previews", "<on|off>", "let the radio queue 30-second Go+ previews"),
    ("cover", "<on|off>", "cover art in the player"),
    ("shuffle", "", "shuffle what's coming up"),
    ("clear", "", "empty the queue (keeps the playing track)"),
    ("history", "clear", "forget your listening history"),
    ("login", "", "sign in — opens a Firefox window"),
    ("logout", "", "sign out and forget the login"),
    ("help", "", "keys and commands"),
    ("quit", "", "exit klangtui"),
]

_ARGS: dict[str, list[tuple[str, str]]] = {
    "search": [("tracks", "only tracks"), ("sets", "only playlists & albums"),
               ("people", "only artists")],
    "repeat": [("off", "play the queue once"), ("all", "loop the queue"),
               ("one", "loop this track")],
    "autoplay": [("on", ""), ("off", "")],
    "previews": [("on", ""), ("off", "")],
    "cover": [("on", ""), ("off", "")],
    "history": [("clear", "forget every play")],
    "volume": [("25", ""), ("50", ""), ("75", ""), ("100", "")],
}


def suggestions(value: str) -> list[tuple[str, str, str]]:
    """(fill, label, description) for the command menu."""
    v = value.lstrip()
    if " " in v:
        word, arg = v.split(" ", 1)
        if " " in arg.strip():
            return []
        a = arg.strip().lower()
        return [(f"{word} {c} ", c, d) for c, d in _ARGS.get(word.lower(), [])
                if c.startswith(a)]
    return [(f"{name} ", f"{name} {args}".strip(), desc)
            for name, args, desc in COMMANDS if name.startswith(v.lower())]


def help_text() -> Text:
    t = Text()
    t.append("keys\n", ACCENT)
    for k, d in KEYS:
        t.append(f"  {k:<16}", YELLOW)
        t.append(f"{d}\n")
    t.append("\ncommands", ACCENT)
    t.append("  (press : first)\n", DIM)
    for name, args, desc in COMMANDS:
        t.append(f"  {name:<9}", YELLOW)
        t.append(f"{args:<28}", DIM)
        t.append(f"{desc}\n")
    t.append("\nmouse: click a tab · click a row twice to play · click the waveform "
             "to seek · scroll the player for volume", DIM)
    return t


def info_text(item: dict) -> Text:
    rows: list[tuple[str, str]] = []
    k = kind_of(item)
    if k == "track":
        rows = [("title", item.get("title") or "?"), ("artist", artist_of(item)),
                ("length", fmt_time(seconds_of(item))),
                ("plays", fmt_count(item.get("playback_count"))),
                ("likes", fmt_count(item.get("likes_count"))),
                ("genre", item.get("genre") or ""),
                ("uploaded", (item.get("created_at") or "")[:10]),
                ("stream", "30-second preview (Go+)" if is_preview(item)
                 else "not available here" if is_blocked(item) else "full"),
                ("url", item.get("permalink_url") or "")]
    elif k == "playlist":
        rows = [("title", item.get("title") or "?"), ("by", artist_of(item)),
                ("tracks", str(item.get("track_count") or len(item.get("tracks") or []))),
                ("url", item.get("permalink_url") or "")]
    elif k == "user":
        rows = [("name", item.get("full_name") or item.get("username") or "?"),
                ("user", "@" + (item.get("permalink") or "?")),
                ("followers", fmt_count(item.get("followers_count"))),
                ("tracks", fmt_count(item.get("track_count"))),
                ("city", ", ".join(x for x in (item.get("city"), item.get("country_code"))
                                   if x)),
                ("url", item.get("permalink_url") or "")]
    t = Text()
    for label, value in rows:
        if value:
            t.append(f"{label:<10}", DIM)
            t.append(f"{value}\n")
    return t


