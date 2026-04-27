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
# Leading episode number with no separator: "003isthislove" → episode=3, stem="isthislove"
_BARE_EPISODE_PREFIX = re.compile(r"^(\d{2,4})\D")
# Hey-Arnold-style split-episode marker: "S01E01a" / "S01E01b" → strip 'a/b' so guessit parses,
# capture the letter so the destination filename keeps the two halves distinct.
_SEGMENT_LETTER = re.compile(r"(?i)(S\d+E\d+)([a-c])(?=\b|[\s._-])")
# Year ranges in folder names mean "season aired from X to Y", not "show premiered in X".
# Examples: "Season 1 (1994-95)", "AVATAR (2005-2014)", "Book 3 - Fire (2007-08)".
# When a parent-dir name has one of these, we suppress year fallback from that dir.
_YEAR_RANGE = re.compile(r"\(\s*\d{4}\s*[-–]\s*\d{2,4}\s*\)")
# Real episode markers — when present, guessit's season+episode came from a real
# marker (S01E01, Episode 5, E12), not a 4-digit-year false split. Used to
# guard against `_fix_year_split` undoing legitimate parses.
_REAL_SE_MARKER = re.compile(r"(?i)(S\d+\s*E\d+|Episode\s*\d+|\bE\d{2,}\b)")


def _fix_year_split(result: dict[str, object], name: str) -> dict[str, object]:
    """Undo guessit's false split of a 4-digit year into season+episode.

    Without explicit ``SxxExx`` markers, guessit treats trailing year-like
    numbers as season/episode (``Blade.Runner.2049.mkv`` → S20E49,
    ``1917.mkv`` → S19E17). When the concatenation of season+episode is a
    plausible year (1900–2099) AND the filename has no real S/E marker,
    drop the split and recover the year.
    """
    season = result.get("season")
    episode = result.get("episode")
    if isinstance(season, list):
        season = season[0] if season else None
    if isinstance(episode, list):
        episode = episode[0] if episode else None
    if season is None or episode is None:
        return result
    if not isinstance(season, (int, float, str)) or not isinstance(episode, (int, float, str)):
        return result
    try:
        s_int = int(season)
        e_int = int(episode)
    except (TypeError, ValueError):
        return result
    candidate = int(f"{s_int:02d}{e_int:02d}")
    if not (1900 <= candidate <= 2099):
        return result
    if _REAL_SE_MARKER.search(name):
        return result
    fixed = dict(result)
    fixed.pop("season", None)
    fixed.pop("episode", None)
    if "year" not in fixed:
        fixed["year"] = candidate
    if fixed.get("type") == "episode":
        fixed["type"] = "movie"
    return fixed


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

    # Dash-separated movie subtitles ("The Punisher - War Zone") get split by
    # guessit into title + alternative_title. Combine them as "Title: Subtitle"
    # so TMDB search finds the full canonical name (e.g. "Punisher: War Zone").
    # TV folder names also produce alternative_title in guessit but it's usually
    # numbered-prefix noise like "08 Series - The Last Airbender", so skip there.
    alt_title = result.get("alternative_title", "")
    if isinstance(alt_title, list):
        alt_title = alt_title[0] if alt_title else ""
    if title and alt_title and raw_type == "movie":
        title = f"{title}: {alt_title}"

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
    raw_file_result = _parse_name(preprocessed_name)
    file_result = _fix_year_split(raw_file_result, preprocessed_name)
    file_was_false_split = file_result is not raw_file_result
    media_type, title, year, season, episode, episode_end = _extract(file_result)

    # When the filename basename is JUST a year (``1986.mkv``, ``1917.mkv``,
    # ``2049.mkv``), the number is the TITLE, not the release year — the
    # movie titled "1986" actually came out in 2018, "1917" in 2019, etc.
    # Use the year as the title and drop the year so TMDB or the parent dir
    # supplies the real release year.
    if year and not title:
        stem = path.stem
        if stem == str(year):
            title = str(year)
            year = None
            if media_type == MediaType.UNKNOWN:
                media_type = MediaType.MOVIE

    dir_result: dict[str, object] = {}

    # Fill gaps using the parent directory name — release groups often put the
    # show title / season / year there even when individual filenames are bare.
    parent_name = path.parent.name
    if parent_name and parent_name not in {".", ""}:
        dir_result = _fix_year_split(_parse_name(parent_name), parent_name)
        _, dir_title, dir_year, dir_season, _, _ = _extract(dir_result)

        # Year ranges in parent names ("Season 1 (1994-95)") give a season-aired
        # year, not the show's premiere year — drop the year so it doesn't leak.
        if _YEAR_RANGE.search(parent_name):
            dir_year = None

        if not title and dir_title:
            title = dir_title
        if not year and dir_year:
            year = dir_year
        # When the file's S/E was a falsely-split year, the file's title is
        # usually missing the year-suffix (file: "Blade Runner" vs parent:
        # "Blade Runner 2049"), and the file's year is the in-title number
        # rather than the real release year. Prefer parent's title/year when
        # the parent identifies as a movie with a year of its own.
        if file_was_false_split and dir_result.get("type") == "movie":
            if dir_title and len(dir_title) > len(title):
                title = dir_title
            if dir_year:
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

        # If the parent is a season folder (has season number but no show title, e.g. "Season 02")
        # AND the file itself had no season number (so it's a bare episode file, not a well-named
        # S01E01 file that already carries the show title), walk up ancestor directories to find
        # the first one that guessit resolves to a meaningful title.
        # This covers Show/Season N/bare-episode.mp4 where the episode filename is just the
        # episode title and the show name lives further up the path.
        if not dir_title and dir_season is not None and not file_result.get("season"):
            for ancestor in path.parents[1:]:
                anc_name = ancestor.name
                if not anc_name or anc_name in {".", ""}:
                    break
                anc_result = _parse_name(anc_name)
                _, anc_title, _, _, _, _ = _extract(anc_result)
                if anc_title:
                    title = anc_title
                    media_type = MediaType.EPISODE
                    break

    # OVA / OAD / ONA → Season 00 (Jellyfin's Specials convention).
    # Run AFTER parent-dir fallback so an OVA in a "Season 02 + Ovas/" folder
    # still routes to S00 instead of being captured by the season-2 folder.
    # Check filename + immediate parent (already parsed); avoids re-parsing.
    if _is_ova(file_result) or _is_ova(dir_result):
        # OVAs belong in the TV library even when guessit guessed "movie"
        # (e.g. Black.Lagoon.OVA.1080p... has no SxxExx and falls to movie type).
        media_type = MediaType.EPISODE
        season = 0

    # Bare numeric episode prefix with no separator: "003isthislove" → episode=3
    # guessit treats these as movie titles when there is no space/dot/dash before the letters.
    if episode is None and media_type == MediaType.EPISODE:
        m = _BARE_EPISODE_PREFIX.match(path.stem)
        if m:
            episode = int(m.group(1))

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
