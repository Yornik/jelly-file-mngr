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


def _quality_tag(guessed: GuessedMedia) -> str:
    """Extract quality string from raw guessit output (e.g. '720p', '1080p')."""
    size = guessed.raw_guess.get("screen_size")
    return str(size) if size else ""


def _episode_destination(
    dest_root: Path,
    match: TmdbMatch,
    guessed: GuessedMedia,
    source: Path,
    rich_names: bool = False,
) -> Path:
    """Jellyfin episode convention: Show Name/Season XX/S01E01.ext

    With rich_names=True: S01E01-Episode Title-Show Name-720p.ext
    """
    show_name = _safe_name(match.title)
    season = guessed.season or 1
    episode = guessed.episode  # intentionally not defaulted — callers must resolve first
    if episode is None:
        raise ValueError(f"episode number is unknown for '{source.name}'")
    season_folder = f"Season {season:02d}"
    ext = source.suffix.lower()

    seg = guessed.segment or ""
    if guessed.episode_end is not None and guessed.episode_end != episode:
        base_code = f"S{season:02d}E{episode:02d}-E{guessed.episode_end:02d}"
    else:
        base_code = f"S{season:02d}E{episode:02d}{seg}"

    if rich_names:
        parts = [base_code]
        if guessed.episode_title:
            parts.append(_safe_name(guessed.episode_title))
        parts.append(show_name)
        quality = _quality_tag(guessed)
        if quality:
            parts.append(quality)
        episode_file = "-".join(parts) + ext
    else:
        episode_file = base_code + ext

    return dest_root / show_name / season_folder / episode_file


def plan_move(
    guessed: GuessedMedia,
    match: TmdbMatch | None,
    dest_root: Path,
    source: Path,
    rich_names: bool = False,
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
        if guessed.episode is None:
            return PlannedMove(
                source=source,
                destination=dest_root,
                media_type=guessed.media_type,
                tmdb_id=match.tmdb_id,
                matched_title=match.title,
                confidence="low",
                skipped=True,
                skip_reason=(
                    f"No episode number found for '{source.name}' — "
                    "run with --interactive to pick manually"
                ),
            )
        destination = _episode_destination(dest_root, match, guessed, source, rich_names)
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

    if source == destination:
        return PlannedMove(
            source=source,
            destination=destination,
            media_type=guessed.media_type,
            tmdb_id=match.tmdb_id,
            matched_title=match.title,
            confidence="high",
            skipped=True,
            skip_reason="Already in the correct Jellyfin location — no action needed",
        )

    if destination.exists():
        return PlannedMove(
            source=source,
            destination=destination,
            media_type=guessed.media_type,
            tmdb_id=match.tmdb_id,
            matched_title=match.title,
            confidence="high",
            skipped=True,
            skip_reason="Destination already occupied by an existing file — skipping duplicate",
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
