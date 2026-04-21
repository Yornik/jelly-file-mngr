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
    episode = guessed.episode  # intentionally not defaulted — callers must resolve first
    if episode is None:
        raise ValueError(f"episode number is unknown for '{source.name}'")
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

    # Group non-skipped moves by destination to detect duplicates.
    # When multiple source files resolve to the same destination (e.g. different language
    # dubs, multiple quality versions all matched to the same TMDB entry), keep the
    # largest file and skip the rest rather than aborting the entire run.
    dest_to_moves: dict[Path, list[PlannedMove]] = {}
    for move in planned_moves:
        if move.skipped:
            plan.skipped.append(move)
        else:
            dest_to_moves.setdefault(move.destination, []).append(move)

    for dest, moves in dest_to_moves.items():
        if len(moves) == 1:
            plan.moves.append(moves[0])
        else:
            # Keep the largest file; skip the rest.
            winner = max(
                moves,
                key=lambda m: m.source.stat().st_size if m.source.exists() else 0,
            )
            plan.moves.append(winner)
            for loser in moves:
                if loser is winner:
                    continue
                plan.skipped.append(
                    PlannedMove(
                        source=loser.source,
                        destination=dest,
                        media_type=loser.media_type,
                        tmdb_id=loser.tmdb_id,
                        matched_title=loser.matched_title,
                        confidence=loser.confidence,
                        skipped=True,
                        skip_reason=(
                            f"Duplicate destination — keeping larger file "
                            f"'{winner.source.name}' for this destination"
                        ),
                    )
                )

    return plan
