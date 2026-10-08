"""SoundCloud's api-v2 over plain HTTPS — the same calls the website makes.

No browser here: the public client_id is read straight out of soundcloud.com's
HTML, and a signed-in session is just the `oauth_token` cookie, sent as an
`Authorization: OAuth …` header. Every method blocks, so the UI calls them from
worker threads; connections are pooled and kept alive between calls.
"""

from __future__ import annotations

import gzip
import http.client
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

API = "https://api-v2.soundcloud.com"
SITE = "https://soundcloud.com"
# A plain desktop Firefox. SoundCloud only looks at it for the HTML page.
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


class ApiError(Exception):
    """SoundCloud answered with something we can't use."""


class AuthError(ApiError):
    """The action needs a (fresh) sign-in."""


class NetError(ApiError):
    """SoundCloud can't be reached at all."""


class PlaybackError(ApiError):
    """This track has no stream we can play."""


@dataclass
class Page:
    """One page of a list, plus the link to the next one (None at the end)."""

    items: list[dict] = field(default_factory=list)
    next_href: str | None = None


# --- item helpers -----------------------------------------------------------

def kind_of(item: dict) -> str:
    """track / playlist / user / ? — system playlists count as playlists."""
    k = item.get("kind") or ""
    if k == "track":
        return "track"
    if k == "user":
        return "user"
    if "playlist" in k or item.get("tracks") is not None:
        return "playlist"
    return "?"


def is_preview(track: dict) -> bool:
    """Go+ tracks that only ship a 30-second snippet."""
    if track.get("policy") == "SNIP":
        return True
    tcs = (track.get("media") or {}).get("transcodings") or []
    return bool(tcs) and all(t.get("snipped") for t in tcs)


def is_blocked(track: dict) -> bool:
    return track.get("policy") == "BLOCK"


def artist_of(item: dict) -> str:
    return (item.get("user") or {}).get("username") or "?"


def seconds_of(track: dict) -> float:
    return (track.get("full_duration") or track.get("duration") or 0) / 1000


_TRACK_KEYS = (
    "id", "kind", "urn", "title", "duration", "full_duration", "permalink_url",
    "artwork_url", "waveform_url", "genre", "playback_count", "likes_count",
    "policy", "monetization_model", "track_authorization", "media", "created_at",
)


def slim(track: dict) -> dict:
    """The parts of a track worth storing (history, queue) — a full one is ~5 KB."""
    d = {k: track[k] for k in _TRACK_KEYS if k in track}
    u = track.get("user") or {}
    d["user"] = {k: u.get(k) for k in ("id", "username", "permalink", "avatar_url")}
    return d


# --- HTTP: a tiny keep-alive pool over http.client --------------------------

class _Pool:
    """Idle HTTPS connections per host, shared by every worker thread."""

    def __init__(self, per_host: int = 4):
        self._idle: dict[str, list[http.client.HTTPSConnection]] = {}
        self._lock = threading.Lock()
        self._per_host = per_host

    def get(self, host: str) -> http.client.HTTPSConnection:
        with self._lock:
            idle = self._idle.get(host)
            if idle:
                return idle.pop()
        return http.client.HTTPSConnection(host, timeout=20)

    def put(self, host: str, conn: http.client.HTTPSConnection):
        with self._lock:
            idle = self._idle.setdefault(host, [])
            if len(idle) < self._per_host:
                idle.append(conn)
                return
        conn.close()

    def drop(self, host: str):
        """Forget every idle connection to host — after one went stale, the
        rest (same idle age) almost certainly did too."""
        with self._lock:
            conns = self._idle.pop(host, [])
        for c in conns:
            c.close()

    def close(self):
        with self._lock:
            conns = [c for idle in self._idle.values() for c in idle]
            self._idle.clear()
        for c in conns:
            c.close()


# a keep-alive socket the server already dropped — reconnect once, silently
_STALE = (http.client.RemoteDisconnected, http.client.CannotSendRequest,
          http.client.BadStatusLine, ConnectionResetError, BrokenPipeError)


class SoundCloud:
    """One SoundCloud session: client_id, optional OAuth token, pooled HTTP."""

    def __init__(self, data_dir: Path, token: str | None = None):
        self.token = token
        self._cid_path = data_dir / "client_id"
        self._cid: str | None = None
        self._cid_lock = threading.Lock()
        self._pool = _Pool()

    def close(self):
        self._pool.close()

    # --- raw HTTP ---

    def _http(self, method: str, url: str, headers: dict | None = None,
              body: bytes | None = None) -> tuple[int, bytes]:
        """One request, following redirects. NetError when the network fails."""
        for _ in range(4):
            parts = urlsplit(url)
            path = parts.path + (f"?{parts.query}" if parts.query else "")
            hdrs = {"User-Agent": UA, "Accept-Encoding": "gzip",
                    "Accept": "application/json, */*", **(headers or {})}
            for attempt in range(2):
                conn = self._pool.get(parts.netloc)
                try:
                    conn.request(method, path or "/", body=body, headers=hdrs)
                    resp = conn.getresponse()
                    data = resp.read()
                except _STALE as e:
                    conn.close()
                    self._pool.drop(parts.netloc)
                    if attempt == 0:
                        continue
                    raise NetError("lost the connection to SoundCloud") from e
                except (OSError, http.client.HTTPException) as e:
                    conn.close()
                    raise NetError("can't reach SoundCloud — check your connection") from e
                break
            if resp.getheader("Content-Encoding") == "gzip":
                try:
                    data = gzip.decompress(data)
                except (OSError, EOFError) as e:
                    conn.close()
                    raise ApiError("SoundCloud sent a broken response") from e
            if resp.will_close:
                conn.close()
            else:
                self._pool.put(parts.netloc, conn)
            loc = resp.getheader("Location")
            if resp.status in (301, 302, 303, 307, 308) and loc:
                url = loc if loc.startswith("http") else f"{parts.scheme}://{parts.netloc}{loc}"
                continue
            return resp.status, data
        raise ApiError("too many redirects")

    def fetch(self, url: str) -> bytes:
        """A plain GET (artwork, waveforms) — raises ApiError on non-200."""
        status, data = self._http("GET", url)
        if status != 200:
            raise ApiError(f"HTTP {status}")
        return data

    # --- client_id ---

    def client_id(self) -> str:
        with self._cid_lock:
            if not self._cid:
                try:
                    self._cid = self._cid_path.read_text().strip() or None
                except OSError:
                    pass
            if not self._cid:
                self._cid = self._discover_client_id()
                self._save_cid()
            return self._cid

    def _refresh_client_id(self, bad: str):
        """The id we used stopped working — fetch a new one (once per bad id)."""
        with self._cid_lock:
            if self._cid != bad:          # another thread already replaced it
                return
            self._cid = self._discover_client_id()
            self._save_cid()

    def _save_cid(self):
        try:
            self._cid_path.write_text(self._cid or "")
        except OSError:
            pass

    def _discover_client_id(self) -> str:
        """The site embeds its api client in the page's hydration JSON; older
        builds only had it inside a JS bundle, so look there as a fallback."""
        status, html = self._http("GET", SITE + "/", {"Accept": "text/html"})
        page = html.decode("utf-8", "replace")
        m = re.search(r'"hydratable":"apiClient","data":\{"id":"([A-Za-z0-9]{16,})"', page)
        if m:
            return m.group(1)
        scripts = re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', page)
        for src in reversed(scripts):
            try:
                js = self.fetch(src).decode("utf-8", "replace")
            except ApiError:
                continue
            m = re.search(r'client_id\s*[:=]\s*"([A-Za-z0-9]{16,})"', js)
            if m:
                return m.group(1)
        raise ApiError(f"couldn't find SoundCloud's client_id (HTTP {status})")

    # --- api-v2 calls ---

    def _url(self, path: str, params: dict, cid: str) -> str:
        base = path if path.startswith("http") else API + path
        parts = urlsplit(base)
        query = dict(parse_qsl(parts.query))
        query.update({k: v for k, v in params.items() if v is not None})
        query["client_id"] = cid
        return f"{parts.scheme}://{parts.netloc}{parts.path}?{urlencode(query)}"

    def _call(self, method: str, path: str, params: dict | None = None,
              headers: dict | None = None, refresh: bool = True) -> tuple[int, bytes]:
        """An api-v2 call with the client_id and token attached. Swaps a dead
        client_id for a fresh one and retries a hiccup once."""
        params = params or {}
        refreshed = not refresh
        retried = False
        while True:
            cid = self.client_id()
            hdrs = {"Origin": SITE, "Referer": SITE + "/", **(headers or {})}
            if self.token:
                hdrs["Authorization"] = f"OAuth {self.token}"
            try:
                status, data = self._http(method, self._url(path, params, cid), hdrs)
            except NetError:
                if retried:
                    raise
                retried = True
                time.sleep(0.6)
                continue
            if status in (401, 403) and not refreshed:
                refreshed = True
                self._refresh_client_id(cid)
                continue
            if status >= 500 and not retried:
                retried = True
                time.sleep(0.6)
                continue
            return status, data

    def get(self, path: str, **params) -> dict | list:
        status, data = self._call("GET", path, params)
        if 200 <= status < 300:
            try:
                return json.loads(data)
            except ValueError as e:
                raise ApiError("SoundCloud sent something that isn't JSON") from e
        if status == 401:
            if self.token:
                raise AuthError("your SoundCloud session expired — :login again")
            raise ApiError("SoundCloud refused the request (401)")
        if status == 403:
            raise ApiError("SoundCloud refused the request (403) — try again in a minute")
        if status == 404:
            raise ApiError("not found on SoundCloud (404)")
        if status == 429:
            raise ApiError("SoundCloud rate limit — wait a little and try again")
        raise ApiError(f"SoundCloud error (HTTP {status})")

    def _page(self, path: str, **params) -> Page:
        j = self.get(path, **params)
        if isinstance(j, list):
            return Page(j, None)
        return Page(j.get("collection") or [], j.get("next_href"))

    def more(self, next_href: str) -> Page:
        """The page after one returned earlier."""
        return self._page(next_href)

    # --- reads ---

    def search(self, q: str, scope: str = "all", limit: int = 30) -> Page:
        path = {"all": "/search", "tracks": "/search/tracks",
                "playlists": "/search/playlists", "people": "/search/users"}[scope]
        page = self._page(path, q=q, limit=limit)
        page.items = [it for it in page.items if kind_of(it) != "?"]
        return page

    def discover(self) -> list[dict]:
        """The front page: [{title, items: [playlist…]}], like the site's."""
        sections = []
        for sel in self._page("/mixed-selections", limit=12).items:
            items = [it for it in (sel.get("items") or {}).get("collection") or []
                     if kind_of(it) == "playlist"]
            if items:
                sections.append({"title": sel.get("title") or "selection", "items": items})
        if not sections:
            raise ApiError("the front page came back empty — try again later")
        return sections

    def tracks(self, ids: list[int]) -> list[dict]:
        """Full track objects for ids, in the order asked (missing ones dropped)."""
        got: dict = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            for t in self.get("/tracks", ids=",".join(str(x) for x in chunk)) or []:
                got[t["id"]] = t
        return [got[i] for i in ids if i in got]

    def hydrate(self, items: list[dict]) -> list[dict]:
        """Playlists mix full tracks with bare {id} stubs — fill in the stubs."""
        stubs = [t["id"] for t in items if not t.get("title")]
        full = {t["id"]: t for t in self.tracks(stubs)} if stubs else {}
        return [t if t.get("title") else full[t["id"]] for t in items
                if t.get("title") or t["id"] in full]

    def playlist(self, pl: dict, cap: int = 200) -> tuple[list[dict], int]:
        """(tracks, how many more weren't loaded) for a playlist or a system
        playlist (charts, stations, mixes)."""
        if "system" in (pl.get("kind") or ""):
            body = pl if pl.get("tracks") else self.get(f"/system-playlists/{pl['urn']}")
        else:
            try:
                body = self.get(f"/playlists/{pl['id']}")
            except ApiError:
                if not pl.get("permalink_url"):
                    raise
                body = self.get("/resolve", url=pl["permalink_url"])
        stubs = body.get("tracks") or []
        total = body.get("track_count") or len(stubs)
        return self.hydrate(stubs[:cap]), max(0, total - min(cap, len(stubs)))

    def user_tracks(self, user_id: int) -> Page:
        return self._page(f"/users/{user_id}/tracks", limit=50)

    def related(self, track_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
        return self._page(f"/tracks/{track_id}/related", limit=limit, offset=offset).items

    def station(self, track_id: int) -> list[dict]:
        """The track's station — what the site's "Station" button plays.
        Mostly stubs, but each carries its policy (SNIP/BLOCK)."""
        urn = f"soundcloud:system-playlists:track-stations:{track_id}"
        return self.get(f"/system-playlists/{urn}").get("tracks") or []

    def resolve(self, url: str) -> dict:
        return self.get("/resolve", url=url)

    # --- the signed-in user ---

    def me(self) -> dict:
        return self.get("/me")

    def likes(self, user_id: int) -> Page:
        page = self._page(f"/users/{user_id}/track_likes", limit=50)
        page.items = [e["track"] for e in page.items if e.get("track")]
        return page

    def more_likes(self, next_href: str) -> Page:
        page = self._page(next_href)
        page.items = [e["track"] for e in page.items if e.get("track")]
        return page

    def liked_ids(self, user_id: int, cap: int = 2000) -> set[int]:
        """Every track id you've liked (up to cap) — so ♥ is right from the start."""
        ids: set[int] = set()
        page = self._page(f"/users/{user_id}/track_likes", limit=200)
        while True:
            ids.update(e["track"]["id"] for e in page.items if e.get("track"))
            if not page.next_href or len(ids) >= cap:
                return ids
            page = self._page(page.next_href)

    def library(self) -> list[dict]:
        out = []
        for it in self._page("/me/library/all", limit=50).items:
            pl = it.get("playlist") or it.get("system_playlist")
            if pl:
                out.append(pl)
        return out

    def set_like(self, user_id: int, track_id: int, on: bool) -> int:
        """PUT/DELETE a like. Returns the HTTP status — 403 means SoundCloud's
        bot-check wants a real browser (the caller falls back to one)."""
        if not self.token:
            raise AuthError("you're not signed in — :login first")
        status, _ = self._call("PUT" if on else "DELETE",
                               f"/users/{user_id}/track_likes/{track_id}",
                               headers={"Accept": "application/json"}, refresh=False)
        if status == 401:
            raise AuthError("your SoundCloud session expired — :login again")
        return status

    def like_url(self, user_id: int, track_id: int) -> str:
        return self._url(f"/users/{user_id}/track_likes/{track_id}", {}, self.client_id())

    # --- playback ---

    # Best first: the plain mp3 is the sturdiest; HLS (AAC 160k, mp3) is what
    # the web player falls back to. Encrypted variants are Go+ DRM — never.
    _STREAM_ORDER = (
        ("progressive", "audio/mpeg"),
        ("hls", "audio/mp4"),
        ("hls", "audio/mpeg"),
        ("hls", "audio/ogg"),
    )

    def _transcodings(self, track: dict) -> list[dict]:
        tcs = [t for t in (track.get("media") or {}).get("transcodings") or []
               if "encrypted" not in (t.get("format") or {}).get("protocol", "")]

        def rank(t):
            fmt = t.get("format") or {}
            key = (fmt.get("protocol"), (fmt.get("mime_type") or "").split(";")[0])
            order = (self._STREAM_ORDER.index(key) if key in self._STREAM_ORDER
                     else len(self._STREAM_ORDER))
            return (bool(t.get("snipped")), order, "96" in (t.get("preset") or ""))
        return sorted(tcs, key=rank)

    def stream(self, track: dict) -> tuple[str, bool, dict]:
        """Track → (signed stream URL, is_30s_preview, full track)."""
        if not (track.get("media") or {}).get("transcodings"):
            track = self.get(f"/tracks/{track['id']}")
        last: Exception | None = None
        for fresh in (False, True):
            if fresh:                     # a stored track's authorization went stale
                track = self.get(f"/tracks/{track['id']}")
            for tc in self._transcodings(track):
                params = {}
                if track.get("track_authorization"):
                    params["track_authorization"] = track["track_authorization"]
                try:
                    j = self.get(tc["url"], **params)
                except AuthError:
                    raise
                except ApiError as e:
                    last = e
                    continue
                if isinstance(j, dict) and j.get("url"):
                    return j["url"], bool(tc.get("snipped")), track
        if is_blocked(track):
            raise PlaybackError("not available in your country")
        raise PlaybackError(f"no playable stream ({last})" if last else "no playable stream")

    def waveform(self, track: dict) -> list[float] | None:
        """The track's real waveform, as 0..1 levels (about 1800 of them)."""
        url = (track.get("waveform_url") or "").replace(".png", ".json")
        if not url.endswith(".json"):
            return None
        try:
            j = json.loads(self.fetch(url))
        except (ApiError, ValueError):
            return None
        h = j.get("height") or 140
        return [max(0.0, min(1.0, s / h)) for s in j.get("samples") or []] or None
