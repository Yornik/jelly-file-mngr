"""SQLite-backed cache for TMDB results and move history.

Stored at ~/.cache/jellyfiler/cache.db by default.
Prevents redundant TMDB API calls across runs and tracks which files
have already been processed so re-runs skip them automatically.
"""

import json
import sqlite3
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

CREATE TABLE IF NOT EXISTS move_log (
    source_path TEXT PRIMARY KEY,
    dest_path   TEXT NOT NULL,
    moved_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Cache:
    def __init__(self, db_path: Path = _DEFAULT_DB) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── TMDB cache ────────────────────────────────────────────────────

    def get_tmdb(
        self, title: str, year: int | None, media_type: MediaType
    ) -> list[TmdbMatch] | None:
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

    # ── Move log ──────────────────────────────────────────────────────

    def already_moved(self, source: Path) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM move_log WHERE source_path=?", (str(source),)
            ).fetchone()
            is not None
        )

    def record_move(self, source: Path, dest: Path) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO move_log (source_path, dest_path)
            VALUES (?, ?)
            """,
            (str(source), str(dest)),
        )
        self._conn.commit()
