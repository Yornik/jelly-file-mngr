"""Parse messy torrent/release filenames using guessit."""

from pathlib import Path

import guessit

from jellyfiler.models import GuessedMedia, MediaType


def _clean_title(title: str) -> str:
    return " ".join(title.split()).strip()


def guess(path: Path) -> GuessedMedia:
    """Parse a file or directory name into structured media metadata.

    guessit handles the heavy lifting of stripping release group noise
    (BluRay, x265, REMUX, ELiTE, etc.) and extracting title/year/episode.
    """
    name = path.name if path.is_file() else path.name
    result = dict(guessit.guessit(name))

    raw_type = result.get("type", "unknown")
    if raw_type == "movie":
        media_type = MediaType.MOVIE
    elif raw_type == "episode":
        media_type = MediaType.EPISODE
    else:
        media_type = MediaType.UNKNOWN

    title = result.get("title", "")
    if isinstance(title, list):
        title = title[0]
    title = _clean_title(str(title)) if title else ""

    year = result.get("year")
    if isinstance(year, list):
        year = year[0]
    year = int(year) if year else None

    season = result.get("season")
    if isinstance(season, list):
        season = season[0]
    season = int(season) if season else None

    episode = result.get("episode")
    if isinstance(episode, list):
        episode = episode[0]
    episode = int(episode) if episode else None

    return GuessedMedia(
        source_path=path,
        media_type=media_type,
        title=title,
        year=year,
        season=season,
        episode=episode,
        episode_title=result.get("episode_title"),
        raw_guess=result,
    )
