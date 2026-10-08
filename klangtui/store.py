"""Everything klangtui remembers between runs, in one SQLite file.

settings · the tracks you met (slim JSON) · every play (when, how long) ·
your liked ids · the queue you left off with.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .api import slim

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    id    INTEGER PRIMARY KEY,
    data  TEXT    NOT NULL,
    seen  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS plays (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id  INTEGER NOT NULL,
    started   INTEGER NOT NULL,
    listened  REAL    NOT NULL DEFAULT 0,
    finished  INTEGER NOT NULL DEFAULT 0,
    source    TEXT
);
CREATE INDEX IF NOT EXISTS plays_started ON plays(started);
CREATE INDEX IF NOT EXISTS plays_track   ON plays(track_id);
CREATE TABLE IF NOT EXISTS likes (
    track_id INTEGER PRIMARY KEY
);
"""

_DAY = 86400


class Store:
    """Thread-safe: workers read history (radio) while the UI writes it."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.executescript(_SCHEMA)
            # tracks nobody refers to any more (not in history, not queued)
            self._con.execute(
                "DELETE FROM tracks WHERE seen < ? AND id NOT IN (SELECT track_id FROM plays)",
                (int(time.time()) - 30 * _DAY,))

    def close(self):
        with self._lock:
            self._con.close()

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._con.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._con.execute(sql, args).lastrowid or 0

    # --- settings ---

    def get(self, key: str, default: str | None = None) -> str | None:
        rows = self._q("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0][0] if rows else default

    def put(self, key: str, value: str):
        self._x("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))

    def get_json(self, key: str, default=None):
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def put_json(self, key: str, value):
        self.put(key, json.dumps(value, separators=(",", ":")))

    def get_int(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(self.get(key, str(default)))))
        except ValueError:
            return default

    # --- tracks ---

    def remember(self, tracks: list[dict]):
        now = int(time.time())
        rows = [(t["id"], json.dumps(slim(t), separators=(",", ":")), now)
                for t in tracks if t.get("id") and t.get("title")]
        with self._lock:
            self._con.executemany(
                "INSERT OR REPLACE INTO tracks(id, data, seen) VALUES(?, ?, ?)", rows)

    def tracks(self, ids: list[int]) -> list[dict]:
        """Stored tracks for ids, in order (unknown ids are skipped)."""
        if not ids:
            return []
        got: dict = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for tid, data in self._q(f"SELECT id, data FROM tracks WHERE id IN ({marks})",
                                     tuple(chunk)):
                got[tid] = json.loads(data)
        return [got[i] for i in ids if i in got]

    # --- listening history ---

    def play_started(self, track: dict, source: str) -> int:
        self.remember([track])
        return self._x("INSERT INTO plays(track_id, started, source) VALUES(?, ?, ?)",
                       (track["id"], int(time.time()), source))

    def play_progress(self, play_id: int, listened: float, finished: bool):
        self._x("UPDATE plays SET listened=?, finished=? WHERE id=?",
                (round(listened, 1), int(finished), play_id))

    def history(self, limit: int = 300) -> list[tuple[dict, int, int]]:
        """[(track, last played at, times played)], most recent first."""
        rows = self._q(
            "SELECT track_id, MAX(started), COUNT(*) FROM plays "
            "GROUP BY track_id ORDER BY MAX(started) DESC LIMIT ?", (limit,))
        by_id = {t["id"]: t for t in self.tracks([r[0] for r in rows])}
        return [(by_id[tid], last, n) for tid, last, n in rows if tid in by_id]

    def played_since(self, seconds: float) -> set[int]:
        since = int(time.time() - seconds)
        return {r[0] for r in self._q("SELECT DISTINCT track_id FROM plays WHERE started >= ?",
                                      (since,))}

    def ever_played(self) -> set[int]:
        return {r[0] for r in self._q("SELECT DISTINCT track_id FROM plays")}

    def clear_history(self):
        self._x("DELETE FROM plays")

    # --- likes (a local mirror of your SoundCloud likes) ---

    def liked_ids(self) -> set[int]:
        return {r[0] for r in self._q("SELECT track_id FROM likes")}

    def set_liked_ids(self, ids: set[int]):
        with self._lock:
            self._con.execute("DELETE FROM likes")
            self._con.executemany("INSERT INTO likes(track_id) VALUES(?)",
                                  [(i,) for i in ids])

    def set_liked(self, track_id: int, liked: bool):
        if liked:
            self._x("INSERT OR IGNORE INTO likes(track_id) VALUES(?)", (track_id,))
        else:
            self._x("DELETE FROM likes WHERE track_id=?", (track_id,))

    # --- the queue you left off with ---

    def save_queue(self, tracks: list[dict], index: int, position: float, mode: str):
        self.remember(tracks)
        self.put_json("queue", {"ids": [t["id"] for t in tracks], "index": index,
                                "position": round(position, 1), "mode": mode})

    def load_queue(self) -> tuple[list[dict], int, float, str]:
        q = self.get_json("queue") or {}
        ids = q.get("ids") or []
        tracks = self.tracks(ids)
        index = q.get("index", -1)
        # a stored track may have vanished — keep pointing at the same song
        if 0 <= index < len(ids) and ids[index] in {t["id"] for t in tracks}:
            index = [t["id"] for t in tracks].index(ids[index])
        else:
            index = 0 if tracks else -1
        return tracks, index, float(q.get("position") or 0), q.get("mode") or "list"
