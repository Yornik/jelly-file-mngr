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
    episode_title: str | None = None
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


@dataclass
class Plan:
    moves: list[PlannedMove] = field(default_factory=list)
    skipped: list[PlannedMove] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.moves) + len(self.skipped)
