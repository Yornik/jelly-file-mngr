"""Data models."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class MediaType(StrEnum):
    MOVIE = "movie"
    EPISODE = "episode"
    UNKNOWN = "unknown"


@dataclass
class GuessedMedia:
    source_path: Path
    media_type: MediaType
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    episode_title: str | None = None
    # Single-letter segment marker for split episodes (Hey Arnold-style "S01E01a", "S01E01b").
    # Two 11-minute halves of one slot — preserved in the destination filename so the two
    # files don't collide on S01E01.mkv.
    segment: str | None = None
    raw_guess: dict[str, object] = field(default_factory=dict)


@dataclass
class TmdbMatch:
    tmdb_id: int
    title: str
    year: int | None
    media_type: MediaType


@dataclass
class PlannedMove:
    source: Path
    destination: Path
    media_type: MediaType
    tmdb_id: int | None
    matched_title: str
    confidence: str  # "high" | "low"
    skipped: bool = False
    skip_reason: str = ""
    # Dedupe identity — independent of quality/resolution. Two PlannedMoves with
    # the same key represent the same canonical content and should be treated as
    # duplicates by the dedupe pass.
    #   Episode: (tmdb_id, season, episode, normalized_title)
    #     — same title at different quality = duplicate
    #     — different segment titles at the same SxxExx (e.g. Animaniacs) = NOT duplicate
    #   Movie:   (tmdb_id, year)
    # When None, falls back to ``destination`` so legacy code paths still work.
    dedupe_key: tuple[object, ...] | None = None


@dataclass
class Plan:
    moves: list[PlannedMove] = field(default_factory=list)
    skipped: list[PlannedMove] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.moves) + len(self.skipped)
