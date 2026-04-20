"""Parse messy torrent/release filenames using guessit."""

import re
from pathlib import Path

import guessit

from jellyfiler.models import GuessedMedia, MediaType

# Lowercase consonant sort prefix, with or without period: "b Superman II" → "Superman II"
# Excludes vowels (a/e/i/o/u) since they can be articles. Uppercase letters are real titles.
_LEADING_PREFIX = re.compile(r"^[b-df-hj-np-tv-z]\.? (?=[A-Z])")
# Quality residue guessit sometimes leaves in titles: "ghostbusters 720bd" → "ghostbusters"
_QUALITY_RESIDUE = re.compile(r"\s+\d{3,4}[bBpP][dD]?\b.*$")


def _clean_title(title: str) -> str:
    title = " ".join(title.split()).strip()
    title = _LEADING_PREFIX.sub("", title).strip()
    title = _QUALITY_RESIDUE.sub("", title).strip()
    # All-caps titles (e.g. "DANNY PHANTOM") confuse some APIs — normalize to title case
    if title and title == title.upper() and title.replace(" ", "").isalpha():
        title = title.title()
    return title


def _parse_name(name: str) -> dict[str, object]:
    return dict(guessit.guessit(name))


def _extract(
    result: dict[str, object],
) -> tuple[MediaType, str, int | None, int | None, int | None]:
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
    year = int(year) if isinstance(year, (int, float, str)) and year else None
    season = result.get("season")
    if isinstance(season, list):
        season = season[0]
    season = int(season) if isinstance(season, (int, float, str)) and season else None
    episode = result.get("episode")
    if isinstance(episode, list):
        episode = episode[0]
    episode = int(episode) if isinstance(episode, (int, float, str)) and episode else None
    return media_type, title, year, season, episode


def guess(path: Path) -> GuessedMedia:
    """Parse a filename (and its parent directory name) into structured media metadata.

    guessit parses the filename first. Any missing fields (title, year, season)
    are filled in from the parent directory name, which often carries the show
    title and season pack info that individual episode files omit.
    """
    file_result = _parse_name(path.name)
    media_type, title, year, season, episode = _extract(file_result)

    # Fill gaps using the parent directory name — release groups often put the
    # show title / season / year there even when individual filenames are bare.
    parent_name = path.parent.name
    if parent_name and parent_name not in {".", ""}:
        dir_result = _parse_name(parent_name)
        _, dir_title, dir_year, dir_season, _ = _extract(dir_result)

        if not title and dir_title:
            title = dir_title
        if not year and dir_year:
            year = dir_year
        if not season and dir_season:
            season = dir_season
        # Prefer file-level media type; fall back to dir if unknown
        if media_type == MediaType.UNKNOWN and dir_result.get("type") != "unknown":
            _, _, _, _, _ = _extract(dir_result)
            raw = dir_result.get("type", "unknown")
            if raw == "movie":
                media_type = MediaType.MOVIE
            elif raw == "episode":
                media_type = MediaType.EPISODE

    return GuessedMedia(
        source_path=path,
        media_type=media_type,
        title=title,
        year=year,
        season=season,
        episode=episode,
        episode_title=str(file_result["episode_title"])
        if file_result.get("episode_title")
        else None,
        raw_guess=file_result,
    )
