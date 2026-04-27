"""SQLite-backed cache for TMDB results and move history.

Stored at ~/.cache/jellyfiler/cache.db by default.
Prevents redundant TMDB API calls across runs and tracks which files
have already been processed so re-runs skip them automatically.

Thread safety
-------------
Connection is opened with ``check_same_thread=False`` so it can be used by
the parallel TMDB lookup pool. Every read/write goes through ``self._lock``
so two workers never touch the connection simultaneously — sqlite would
deadlock under concurrent writes from the same connection.
"""

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from jellyfiler.models import MediaType, TmdbMatch

_DEFAULT_DB = Path.home() / ".cache" / "jellyfiler" / "cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tmdb_cache (
    title_lower TEXT NOT NULL,
    year        INTEGER,
    media_type  TEXT NOT NULL,
    results_json TEXT NOT NULL,
    cached_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (title_lower, year, media_type)
);

CREATE TABLE IF NOT EXISTS tmdb_pinned (
    title_lower TEXT NOT NULL,
    year        INTEGER,
    media_type  TEXT NOT NULL,
    match_json  TEXT NOT NULL,
    pinned_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (title_lower, year, media_type)
);

CREATE TABLE IF NOT EXISTS move_log (
    source_path TEXT PRIMARY KEY,
    dest_path   TEXT NOT NULL,
    moved_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Cache:
    def __init__(self, db_path: Path = _DEFAULT_DB) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is required for the parallel TMDB lookup pool;
        # all access is serialised by self._lock below.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── TMDB cache ────────────────────────────────────────────────────

    def get_tmdb(
        self, title: str, year: int | None, media_type: MediaType
    ) -> list[TmdbMatch] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT results_json FROM tmdb_cache WHERE title_lower=? AND year IS ? AND media_type=?",
                (title.lower(), year, media_type.value),
            ).fetchone()
        if row is None:
            return None
        raw: list[dict[str, Any]] = json.loads(row[0])
        return [
            TmdbMatch(
                tmdb_id=int(r["tmdb_id"]),
                title=str(r["title"]),
                year=int(r["year"]) if r.get("year") is not None else None,
                media_type=MediaType(str(r["media_type"])),
            )
            for r in raw
        ]

    def set_tmdb(
        self, title: str, year: int | None, media_type: MediaType, matches: list[TmdbMatch]
    ) -> None:
        serialised = json.dumps(
            [
                {
                    "tmdb_id": m.tmdb_id,
                    "title": m.title,
                    "year": m.year,
                    "media_type": m.media_type.value,
                }
                for m in matches
            ]
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tmdb_cache (title_lower, year, media_type, results_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(title_lower, year, media_type)
                DO UPDATE SET results_json=excluded.results_json, cached_at=datetime('now')
                """,
                (title.lower(), year, media_type.value, serialised),
            )
            self._conn.commit()

    # ── Pinned choices (user-confirmed interactive picks) ─────────────

    def get_pinned(self, title: str, year: int | None, media_type: MediaType) -> TmdbMatch | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT match_json FROM tmdb_pinned WHERE title_lower=? AND year IS ? AND media_type=?",
                (title.lower(), year, media_type.value),
            ).fetchone()
        if row is None:
            return None
        r: dict[str, Any] = json.loads(row[0])
        return TmdbMatch(
            tmdb_id=int(r["tmdb_id"]),
            title=str(r["title"]),
            year=int(r["year"]) if r.get("year") is not None else None,
            media_type=MediaType(str(r["media_type"])),
        )

    def set_pinned(
        self, title: str, year: int | None, media_type: MediaType, match: TmdbMatch
    ) -> None:
        serialised = json.dumps(
            {
                "tmdb_id": match.tmdb_id,
                "title": match.title,
                "year": match.year,
                "media_type": match.media_type.value,
            }
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tmdb_pinned (title_lower, year, media_type, match_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(title_lower, year, media_type)
                DO UPDATE SET match_json=excluded.match_json, pinned_at=datetime('now')
                """,
                (title.lower(), year, media_type.value, serialised),
            )
            self._conn.commit()

    # ── Move log ──────────────────────────────────────────────────────

    def already_moved(self, source: Path) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM move_log WHERE source_path=?", (str(source),)
                ).fetchone()
                is not None
            )

    def record_move(self, source: Path, dest: Path) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO move_log (source_path, dest_path)
                VALUES (?, ?)
                """,
                (str(source), str(dest)),
            )
            self._conn.commit()

    # ── Stats & management ────────────────────────────────────────────

    def stats(self) -> dict[str, int]:
        """Return row counts for each table."""
        with self._lock:
            return {
                "tmdb_cache": self._conn.execute("SELECT COUNT(*) FROM tmdb_cache").fetchone()[0],
                "pinned": self._conn.execute("SELECT COUNT(*) FROM tmdb_pinned").fetchone()[0],
                "move_log": self._conn.execute("SELECT COUNT(*) FROM move_log").fetchone()[0],
            }

    def unpin(self, title: str, year: int | None, media_type: MediaType) -> bool:
        """Remove a single pinned entry. Returns True if a row was deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM tmdb_pinned WHERE title_lower=? AND year IS ? AND media_type=?",
                (title.lower(), year, media_type.value),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def clear(
        self,
        *,
        pinned: bool = False,
        tmdb: bool = False,
        moves: bool = False,
    ) -> dict[str, int]:
        """Delete rows from the selected tables. Returns deleted row counts."""
        deleted: dict[str, int] = {}
        with self._lock:
            if pinned:
                cur = self._conn.execute("DELETE FROM tmdb_pinned")
                deleted["pinned"] = cur.rowcount
            if tmdb:
                cur = self._conn.execute("DELETE FROM tmdb_cache")
                deleted["tmdb_cache"] = cur.rowcount
            if moves:
                cur = self._conn.execute("DELETE FROM move_log")
                deleted["move_log"] = cur.rowcount
            self._conn.commit()
        return deleted
