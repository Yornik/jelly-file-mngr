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
# Hey-Arnold-style split-episode marker: "S01E01a" / "S01E01b" → strip 'a/b' so guessit parses,
# capture the letter so the destination filename keeps the two halves distinct.
_SEGMENT_LETTER = re.compile(r"(?i)(S\d+E\d+)([a-c])(?=\b|[\s._-])")


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


def _is_ova(result: dict[str, object]) -> bool:
    """True when guessit flagged the name as an OVA / OAD / ONA.

    guessit puts 'Original Animated Video' in the 'other' field whenever it sees
    OVA, OVAs, OAD, ONA tokens in the release name. We use that as the signal
    to route the file to Jellyfin's Season 00 (Specials).
    """
    other = result.get("other")
    if isinstance(other, str):
        return "Original Animated Video" in other
    if isinstance(other, list):
        return any("Original Animated Video" in str(o) for o in other)
    return False


def _extract(
    result: dict[str, object],
) -> tuple[MediaType, str, int | None, int | None, int | None, int | None]:
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
    episode_raw = result.get("episode")
    episode: int | None
    episode_end: int | None
    if isinstance(episode_raw, list) and len(episode_raw) > 1:
        episode = int(min(episode_raw))
        episode_end = int(max(episode_raw))
    elif isinstance(episode_raw, list):
        episode = int(episode_raw[0]) if episode_raw else None
        episode_end = None
    else:
        episode = (
            int(episode_raw) if isinstance(episode_raw, (int, float, str)) and episode_raw else None
        )
        episode_end = None
    return media_type, title, year, season, episode, episode_end


def _strip_segment_letter(name: str) -> tuple[str, str | None]:
    """Strip a single-letter split-episode marker so guessit can parse SxxExx.

    Returns (stripped_name, letter or None). 'S01E01a Title.mkv' → ('S01E01 Title.mkv', 'a').
    """
    m = _SEGMENT_LETTER.search(name)
    if not m:
        return name, None
    return name[: m.start(2)] + name[m.end(2) :], m.group(2).lower()


def guess(path: Path) -> GuessedMedia:
    """Parse a filename (and its parent directory name) into structured media metadata.

    guessit parses the filename first. Any missing fields (title, year, season)
    are filled in from the parent directory name, which often carries the show
    title and season pack info that individual episode files omit.
    """
    # Pre-process: split-episode letter markers (S01E01a/b) confuse guessit.
    preprocessed_name, segment = _strip_segment_letter(path.name)
    file_result = _parse_name(preprocessed_name)
    media_type, title, year, season, episode, episode_end = _extract(file_result)

    dir_result: dict[str, object] = {}

    # Fill gaps using the parent directory name — release groups often put the
    # show title / season / year there even when individual filenames are bare.
    parent_name = path.parent.name
    if parent_name and parent_name not in {".", ""}:
        dir_result = _parse_name(parent_name)
        _, dir_title, dir_year, dir_season, _, _ = _extract(dir_result)

        if not title and dir_title:
            title = dir_title
        if not year and dir_year:
            year = dir_year
        # Use `is None` so an explicit season=0 (S00E01) isn't replaced.
        if season is None and dir_season is not None:
            season = dir_season
        # Prefer file-level media type; fall back to dir if unknown
        if media_type == MediaType.UNKNOWN and dir_result.get("type") != "unknown":
            raw = dir_result.get("type", "unknown")
            if raw == "movie":
                media_type = MediaType.MOVIE
            elif raw == "episode":
                media_type = MediaType.EPISODE

    # OVA / OAD / ONA → Season 00 (Jellyfin's Specials convention).
    # Run AFTER parent-dir fallback so an OVA in a "Season 02 + Ovas/" folder
    # still routes to S00 instead of being captured by the season-2 folder.
    # Check filename + immediate parent (already parsed); avoids re-parsing.
    if _is_ova(file_result) or _is_ova(dir_result):
        # OVAs belong in the TV library even when guessit guessed "movie"
        # (e.g. Black.Lagoon.OVA.1080p... has no SxxExx and falls to movie type).
        media_type = MediaType.EPISODE
        season = 0

    return GuessedMedia(
        source_path=path,
        media_type=media_type,
        title=title,
        year=year,
        season=season,
        episode=episode,
        episode_end=episode_end,
        episode_title=str(file_result["episode_title"])
        if file_result.get("episode_title")
        else None,
        segment=segment,
        raw_guess=file_result,
    )
