"""Radio that keeps finding new music instead of looping the same 50 tracks.

SoundCloud's /related alone is narrow: for a big artist most of it is that
artist again, for a niche track it's a handful of songs. So a batch mixes the
track's *station* (what the site's Station button plays) with /related, seeds
from the last few tracks you actually listened to (so the radio drifts), skips
what you heard recently, caps each artist, and drops 30-second previews.
"""

from __future__ import annotations

import random
import re

from .api import ApiError, SoundCloud, is_blocked, is_preview

_RECENT_DAYS = 3          # anything played this recently is skipped
_PER_ARTIST = 2           # at most this many tracks per artist in one batch


def _artist_id(t: dict):
    """Re-uploads live on other accounts ("Playboi Carti" vs "playboicarti"),
    so artists are told apart by their squashed name, not the account id."""
    u = t.get("user") or {}
    name = re.sub(r"[^a-z0-9]", "", (u.get("username") or "").lower())
    return name or u.get("id")


def build_batch(api: SoundCloud, seeds: list[dict], *, queued: set[int],
                recent: set[int], heard: set[int], want: int = 25,
                previews: bool = False) -> list[dict]:
    """Up to `want` fresh tracks around `seeds` (the first one weighs most).

    queued — ids already in the queue (never repeated)
    recent — played in the last few days (skipped unless the radio runs dry)
    heard  — ever played (allowed, but ranked lower than new music)
    """
    # gather candidates, remembering how early each source ranked them
    ranked: dict[int, tuple[float, dict]] = {}

    def offer(tracks: list[dict], weight: float):
        for pos, t in enumerate(tracks):
            tid = t.get("id")
            if not tid or tid in queued:
                continue
            if is_blocked(t) or (not previews and is_preview(t)):
                continue
            score = weight / (1 + pos * 0.04) * random.uniform(0.75, 1.0)
            if tid in heard:
                score *= 0.35
            prev = ranked.get(tid)
            # a track several sources agree on gets a nudge up
            ranked[tid] = (score + (prev[0] * 0.5 if prev else 0), t)

    errors = 0
    for n, seed in enumerate(seeds[:3]):
        w = 1.0 if n == 0 else 0.7
        sid = seed.get("id")
        try:
            offer(api.station(sid), w)
        except ApiError:
            errors += 1
        try:
            # a random later page now and then keeps repeat presses varied
            offset = random.choice((0, 0, 0, 25))
            offer(api.related(sid, limit=50, offset=offset), w * 0.9)
        except ApiError:
            errors += 1
    if not ranked and errors:
        raise ApiError("the radio couldn't reach SoundCloud")

    order = sorted(ranked.values(), key=lambda x: -x[0])
    fresh = [t for _, t in order if t["id"] not in recent]
    stale = [t for _, t in order if t["id"] in recent]
    # station entries are mostly bare stubs: fill in the best ~80 in two calls
    pool = api.hydrate((fresh + stale)[:80])
    pool = [t for t in pool if not is_blocked(t) and (previews or not is_preview(t))]

    seed_artists = {_artist_id(s) for s in seeds[:1]}
    picked: list[dict] = []
    picked_ids: set[int] = set()
    per_artist: dict = {}
    last_artist = None
    for relax in (False, True):          # second pass: let the recent ones in
        for t in pool:
            if len(picked) >= want:
                break
            if t["id"] in picked_ids or (not relax and t["id"] in recent):
                continue
            a = _artist_id(t)
            cap = 1 if a in seed_artists else _PER_ARTIST
            if per_artist.get(a, 0) >= cap or a == last_artist:
                continue
            picked.append(t)
            picked_ids.add(t["id"])
            per_artist[a] = per_artist.get(a, 0) + 1
            last_artist = a
        if len(picked) >= want // 2:
            break
    return picked
