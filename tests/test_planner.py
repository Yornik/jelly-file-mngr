"""Tests for move planning logic."""

from pathlib import Path

from jellyfiler.models import GuessedMedia, MediaType, Plan, PlannedMove, TmdbMatch
from jellyfiler.planner import (
    _episode_destination,
    _movie_destination,
    _safe_name,
    build_plan,
    plan_move,
)


def test_safe_name_strips_bad_chars():
    result = _safe_name('Movie: "The" <End>')
    assert ":" not in result
    assert '"' not in result
    assert "<" not in result
    assert "End" in result


def test_safe_name_strips_trailing_dots():
    assert _safe_name("Movie.") == "Movie"


def test_movie_destination():
    match = TmdbMatch(tmdb_id=1, title="Blade Runner 2049", year=2017, media_type=MediaType.MOVIE)
    source = Path("Blade.Runner.2049.mkv")
    dest = _movie_destination(Path("/dest"), match, source)
    assert dest == Path("/dest/Blade Runner 2049 (2017)/Blade Runner 2049 (2017).mkv")


def test_movie_destination_no_year():
    match = TmdbMatch(tmdb_id=2, title="Coco", year=None, media_type=MediaType.MOVIE)
    source = Path("Coco.mkv")
    dest = _movie_destination(Path("/dest"), match, source)
    assert dest == Path("/dest/Coco/Coco.mkv")


def test_episode_destination():
    match = TmdbMatch(tmdb_id=3, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    guessed = GuessedMedia(
        source_path=Path("Futurama.S12E03.mkv"),
        media_type=MediaType.EPISODE,
        title="Futurama",
        season=12,
        episode=3,
    )
    dest = _episode_destination(Path("/dest"), match, guessed, Path("Futurama.S12E03.mkv"))
    assert dest == Path("/dest/Futurama/Season 12/S12E03.mkv")


def test_episode_destination_pads_single_digit():
    match = TmdbMatch(tmdb_id=3, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    guessed = GuessedMedia(
        source_path=Path("Futurama.S01E01.mkv"),
        media_type=MediaType.EPISODE,
        title="Futurama",
        season=1,
        episode=1,
    )
    dest = _episode_destination(Path("/dest"), match, guessed, Path("Futurama.S01E01.mkv"))
    assert dest == Path("/dest/Futurama/Season 01/S01E01.mkv")


# ---------------------------------------------------------------------------
# plan_move
# ---------------------------------------------------------------------------


def _guessed(
    media_type: MediaType,
    title: str = "Test",
    season: int | None = None,
    episode: int | None = None,
) -> GuessedMedia:
    return GuessedMedia(
        source_path=Path(f"{title}.mkv"),
        media_type=media_type,
        title=title,
        season=season,
        episode=episode,
    )


def test_plan_move_no_match_is_skipped():
    guessed = _guessed(MediaType.MOVIE, "Blade Runner")
    result = plan_move(guessed, None, Path("/dest"), Path("Blade.Runner.mkv"))
    assert result.skipped
    assert "No TMDB match" in result.skip_reason


def test_plan_move_movie_sets_destination():
    guessed = _guessed(MediaType.MOVIE, "Coco")
    match = TmdbMatch(tmdb_id=1, title="Coco", year=2017, media_type=MediaType.MOVIE)
    result = plan_move(guessed, match, Path("/dest"), Path("Coco.mkv"))
    assert not result.skipped
    assert result.destination == Path("/dest/Coco (2017)/Coco (2017).mkv")
    assert result.confidence == "high"
    assert result.tmdb_id == 1


def test_plan_move_episode_sets_destination():
    guessed = _guessed(MediaType.EPISODE, "Futurama", season=12, episode=3)
    match = TmdbMatch(tmdb_id=2, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    result = plan_move(guessed, match, Path("/dest"), Path("Futurama.S12E03.mkv"))
    assert not result.skipped
    assert result.destination == Path("/dest/Futurama/Season 12/S12E03.mkv")


def test_plan_move_unknown_type_is_skipped():
    guessed = _guessed(MediaType.UNKNOWN)
    match = TmdbMatch(tmdb_id=3, title="Test", year=None, media_type=MediaType.UNKNOWN)
    result = plan_move(guessed, match, Path("/dest"), Path("unknown.mkv"))
    assert result.skipped
    assert "media type" in result.skip_reason


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def _planned_move(skipped: bool = False) -> PlannedMove:
    return PlannedMove(
        source=Path("src.mkv"),
        destination=Path("dst.mkv"),
        media_type=MediaType.MOVIE,
        tmdb_id=1,
        matched_title="Test",
        confidence="high",
        skipped=skipped,
        skip_reason="reason" if skipped else "",
    )


def test_build_plan_splits_moves_and_skipped():
    moves = [_planned_move(skipped=False), _planned_move(skipped=False)]
    skips = [_planned_move(skipped=True)]
    plan = build_plan(moves + skips)
    assert len(plan.moves) == 2
    assert len(plan.skipped) == 1


def test_build_plan_empty():
    plan = build_plan([])
    assert plan.moves == []
    assert plan.skipped == []


# ---------------------------------------------------------------------------
# Plan.total
# ---------------------------------------------------------------------------


def test_plan_total():
    plan = Plan(
        moves=[_planned_move(), _planned_move()],
        skipped=[_planned_move(skipped=True)],
    )
    assert plan.total == 3
