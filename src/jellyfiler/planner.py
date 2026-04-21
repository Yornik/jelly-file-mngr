"""Build a rename/move plan from guessed metadata and TMDB matches."""

import re
from pathlib import Path

from jellyfiler.models import GuessedMedia, MediaType, Plan, PlannedMove, TmdbMatch


def _safe_name(name: str) -> str:
    """Strip characters that are unsafe in file/directory names."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return name.strip(" .")


def _movie_destination(dest_root: Path, match: TmdbMatch, source: Path) -> Path:
    """Jellyfin movie convention: Movie Name (Year)/Movie.Name.Year.ext"""
    folder_name = _safe_name(f"{match.title} ({match.year})" if match.year else match.title)
    return dest_root / folder_name / f"{folder_name}{source.suffix.lower()}"


def _episode_destination(
    dest_root: Path,
    match: TmdbMatch,
    guessed: GuessedMedia,
    source: Path,
) -> Path:
    """Jellyfin episode convention: Show Name/Season XX/S01E01.ext"""
    show_name = _safe_name(match.title)
    season = guessed.season or 1
    episode = guessed.episode or 1
    season_folder = f"Season {season:02d}"
    if guessed.episode_end is not None and guessed.episode_end != episode:
        episode_file = (
            f"S{season:02d}E{episode:02d}-E{guessed.episode_end:02d}{source.suffix.lower()}"
        )
    else:
        episode_file = f"S{season:02d}E{episode:02d}{source.suffix.lower()}"
    return dest_root / show_name / season_folder / episode_file


def plan_move(
    guessed: GuessedMedia,
    match: TmdbMatch | None,
    dest_root: Path,
    source: Path,
) -> PlannedMove:
    if not match:
        return PlannedMove(
            source=source,
            destination=dest_root,
            media_type=guessed.media_type,
            tmdb_id=None,
            matched_title=guessed.title,
            confidence="low",
            skipped=True,
            skip_reason=f"No TMDB match found for '{guessed.title}'",
        )

    if guessed.media_type == MediaType.MOVIE:
        destination = _movie_destination(dest_root, match, source)
    elif guessed.media_type == MediaType.EPISODE:
        destination = _episode_destination(dest_root, match, guessed, source)
    else:
        return PlannedMove(
            source=source,
            destination=dest_root,
            media_type=guessed.media_type,
            tmdb_id=None,
            matched_title=guessed.title,
            confidence="low",
            skipped=True,
            skip_reason=f"Cannot determine media type for '{source.name}'",
        )

    return PlannedMove(
        source=source,
        destination=destination,
        media_type=guessed.media_type,
        tmdb_id=match.tmdb_id,
        matched_title=match.title,
        confidence="high",
    )


def build_plan(planned_moves: list[PlannedMove]) -> Plan:
    plan = Plan()
    for move in planned_moves:
        if move.skipped:
            plan.skipped.append(move)
        else:
            plan.moves.append(move)
    return plan
