"""Tests for move planning logic."""

from pathlib import Path

import pytest

from jellyfiler.models import GuessedMedia, MediaType, Plan, PlannedMove, TmdbMatch
from jellyfiler.planner import (
    _episode_destination,
    _movie_destination,
    _quality_tag,
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


def test_episode_destination_multi_episode():
    match = TmdbMatch(tmdb_id=3, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    guessed = GuessedMedia(
        source_path=Path("Futurama.S03E01E02.mkv"),
        media_type=MediaType.EPISODE,
        title="Futurama",
        season=3,
        episode=1,
        episode_end=2,
    )
    dest = _episode_destination(Path("/dest"), match, guessed, Path("Futurama.S03E01E02.mkv"))
    assert dest == Path("/dest/Futurama/Season 03/S03E01-E02.mkv")


def test_episode_destination_multi_episode_three():
    match = TmdbMatch(tmdb_id=3, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    guessed = GuessedMedia(
        source_path=Path("Show.S03E01E02E03.mkv"),
        media_type=MediaType.EPISODE,
        title="Show",
        season=3,
        episode=1,
        episode_end=3,
    )
    dest = _episode_destination(Path("/dest"), match, guessed, Path("Show.S03E01E02E03.mkv"))
    assert dest == Path("/dest/Futurama/Season 03/S03E01-E03.mkv")


def test_episode_destination_raises_when_episode_is_none():
    """Defensive: callers must resolve episode before calling _episode_destination directly."""
    match = TmdbMatch(tmdb_id=3, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    guessed = GuessedMedia(
        source_path=Path("orphan.mkv"),
        media_type=MediaType.EPISODE,
        title="Futurama",
        season=1,
        episode=None,
    )
    with pytest.raises(ValueError, match="episode number is unknown"):
        _episode_destination(Path("/dest"), match, guessed, Path("orphan.mkv"))


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


def test_plan_move_already_at_destination_is_skipped():
    """File already in the correct Jellyfin location — no action needed."""
    guessed = _guessed(MediaType.MOVIE, "Coco", season=None, episode=None)
    match = TmdbMatch(tmdb_id=1, title="Coco", year=2017, media_type=MediaType.MOVIE)
    # source == computed destination → already organised
    dest_root = Path("/dest")
    source = dest_root / "Coco (2017)" / "Coco (2017).mkv"
    result = plan_move(guessed, match, dest_root, source)
    assert result.skipped
    assert "Already in the correct" in result.skip_reason
    assert result.confidence == "high"  # matched, just already there


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


def test_plan_move_episode_no_episode_number_is_skipped():
    """Episode matched on TMDB but filename had no S/E marker — should skip, not default to E01."""
    guessed = _guessed(MediaType.EPISODE, "Futurama", season=3, episode=None)
    match = TmdbMatch(tmdb_id=2, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    result = plan_move(guessed, match, Path("/dest"), Path("Luck of the Fryrish.mkv"))
    assert result.skipped
    assert "No episode number" in result.skip_reason
    assert "--interactive" in result.skip_reason


def test_plan_move_unknown_type_is_skipped():
    guessed = _guessed(MediaType.UNKNOWN)
    match = TmdbMatch(tmdb_id=3, title="Test", year=None, media_type=MediaType.UNKNOWN)
    result = plan_move(guessed, match, Path("/dest"), Path("unknown.mkv"))
    assert result.skipped
    assert "media type" in result.skip_reason


# ---------------------------------------------------------------------------
# fancy_title
# ---------------------------------------------------------------------------


def test_fancy_title_episode_all_fields():
    """S01E01-Episode Title-Show Name-720p.mkv when all fields present."""
    match = TmdbMatch(tmdb_id=3, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    guessed = GuessedMedia(
        source_path=Path("Futurama.S01E01.720p.mkv"),
        media_type=MediaType.EPISODE,
        title="Futurama",
        season=1,
        episode=1,
        episode_title="Space Pilot 3000",
        raw_guess={"screen_size": "720p"},
    )
    dest = _episode_destination(
        Path("/dest"), match, guessed, Path("Futurama.S01E01.720p.mkv"), fancy_title=True
    )
    assert dest == Path("/dest/Futurama/Season 01/S01E01-Space Pilot 3000-Futurama-720p.mkv")


def test_fancy_title_episode_no_episode_title():
    """S01E01-Show Name-720p.mkv when episode title is absent."""
    match = TmdbMatch(tmdb_id=3, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    guessed = GuessedMedia(
        source_path=Path("Futurama.S01E01.720p.mkv"),
        media_type=MediaType.EPISODE,
        title="Futurama",
        season=1,
        episode=1,
        episode_title=None,
        raw_guess={"screen_size": "720p"},
    )
    dest = _episode_destination(
        Path("/dest"), match, guessed, Path("Futurama.S01E01.720p.mkv"), fancy_title=True
    )
    assert dest == Path("/dest/Futurama/Season 01/S01E01-Futurama-720p.mkv")


def test_fancy_title_episode_no_quality():
    """S01E01-Episode Title-Show Name.mkv when no screen_size in raw_guess."""
    match = TmdbMatch(tmdb_id=3, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    guessed = GuessedMedia(
        source_path=Path("Futurama.S01E01.mkv"),
        media_type=MediaType.EPISODE,
        title="Futurama",
        season=1,
        episode=1,
        episode_title="Space Pilot 3000",
        raw_guess={},
    )
    dest = _episode_destination(
        Path("/dest"), match, guessed, Path("Futurama.S01E01.mkv"), fancy_title=True
    )
    assert dest == Path("/dest/Futurama/Season 01/S01E01-Space Pilot 3000-Futurama.mkv")


def test_fancy_title_false_gives_plain_code():
    """Default (fancy_title=False) still produces plain S01E01.mkv."""
    match = TmdbMatch(tmdb_id=3, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    guessed = GuessedMedia(
        source_path=Path("Futurama.S01E01.720p.mkv"),
        media_type=MediaType.EPISODE,
        title="Futurama",
        season=1,
        episode=1,
        episode_title="Space Pilot 3000",
        raw_guess={"screen_size": "720p"},
    )
    dest = _episode_destination(
        Path("/dest"), match, guessed, Path("Futurama.S01E01.720p.mkv"), fancy_title=False
    )
    assert dest == Path("/dest/Futurama/Season 01/S01E01.mkv")


def test_quality_tag_present():
    guessed = GuessedMedia(
        source_path=Path("x.mkv"),
        media_type=MediaType.EPISODE,
        title="X",
        raw_guess={"screen_size": "1080p"},
    )
    assert _quality_tag(guessed) == "1080p"


def test_quality_tag_absent():
    guessed = GuessedMedia(
        source_path=Path("x.mkv"),
        media_type=MediaType.EPISODE,
        title="X",
        raw_guess={},
    )
    assert _quality_tag(guessed) == ""


def test_dedupe_key_episode_includes_normalised_title():
    """Multi-segment slots get different keys; quality is excluded."""
    from jellyfiler.planner import _dedupe_key

    match = TmdbMatch(tmdb_id=42, title="Show", year=2020, media_type=MediaType.EPISODE)
    seg_a = GuessedMedia(
        source_path=Path("a.1080p.mkv"),
        media_type=MediaType.EPISODE,
        title="Show",
        season=3,
        episode=8,
        episode_title="Warners",
        raw_guess={"screen_size": "1080p"},
    )
    seg_b = GuessedMedia(
        source_path=Path("b.720p.mkv"),
        media_type=MediaType.EPISODE,
        title="Show",
        season=3,
        episode=8,
        episode_title="The Flame Returns",
        raw_guess={"screen_size": "720p"},
    )
    assert _dedupe_key(seg_a, match) != _dedupe_key(seg_b, match)


def test_dedupe_key_excludes_quality():
    """Same episode at different qualities → SAME key (duplicates)."""
    from jellyfiler.planner import _dedupe_key

    match = TmdbMatch(tmdb_id=42, title="Show", year=2020, media_type=MediaType.EPISODE)
    high = GuessedMedia(
        source_path=Path("a.1080p.mkv"),
        media_type=MediaType.EPISODE,
        title="Show",
        season=1,
        episode=1,
        episode_title="Pilot",
        raw_guess={"screen_size": "1080p"},
    )
    low = GuessedMedia(
        source_path=Path("b.720p.mkv"),
        media_type=MediaType.EPISODE,
        title="Show",
        season=1,
        episode=1,
        episode_title="Pilot",
        raw_guess={"screen_size": "720p"},
    )
    assert _dedupe_key(high, match) == _dedupe_key(low, match)


def test_dedupe_key_normalises_title_case_and_whitespace():
    from jellyfiler.planner import _dedupe_key

    match = TmdbMatch(tmdb_id=42, title="Show", year=2020, media_type=MediaType.EPISODE)
    a = GuessedMedia(
        source_path=Path("a.mkv"),
        media_type=MediaType.EPISODE,
        title="Show",
        season=1,
        episode=1,
        episode_title="Pilot",
    )
    b = GuessedMedia(
        source_path=Path("b.mkv"),
        media_type=MediaType.EPISODE,
        title="Show",
        season=1,
        episode=1,
        episode_title="  pilot  ",
    )
    assert _dedupe_key(a, match) == _dedupe_key(b, match)


def test_dedupe_key_movie_uses_tmdb_id_and_year():
    from jellyfiler.planner import _dedupe_key

    match = TmdbMatch(tmdb_id=99, title="Coco", year=2017, media_type=MediaType.MOVIE)
    a = GuessedMedia(
        source_path=Path("a.1080p.mkv"), media_type=MediaType.MOVIE, title="Coco", year=2017
    )
    b = GuessedMedia(
        source_path=Path("b.720p.mkv"), media_type=MediaType.MOVIE, title="Coco", year=2017
    )
    assert _dedupe_key(a, match) == _dedupe_key(b, match)


def test_plan_move_populates_dedupe_key():
    """plan_move must set dedupe_key on the returned PlannedMove."""
    guessed = _guessed(MediaType.EPISODE, "Futurama", season=12, episode=3)
    guessed.episode_title = "Bender's Bender"
    match = TmdbMatch(tmdb_id=2, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    result = plan_move(guessed, match, Path("/dest"), Path("Futurama.S12E03.mkv"))
    assert result.dedupe_key is not None
    assert result.dedupe_key[0] == "episode"
    assert result.dedupe_key[1] == 2  # tmdb_id


def test_plan_move_fancy_title_episode():
    """plan_move passes fancy_title=True through to the destination."""
    guessed = GuessedMedia(
        source_path=Path("Futurama.S12E03.720p.mkv"),
        media_type=MediaType.EPISODE,
        title="Futurama",
        season=12,
        episode=3,
        episode_title="Bendless Love",
        raw_guess={"screen_size": "720p"},
    )
    match = TmdbMatch(tmdb_id=2, title="Futurama", year=1999, media_type=MediaType.EPISODE)
    result = plan_move(
        guessed, match, Path("/dest"), Path("Futurama.S12E03.720p.mkv"), fancy_title=True
    )
    assert not result.skipped
    assert result.destination == Path(
        "/dest/Futurama/Season 12/S12E03-Bendless Love-Futurama-720p.mkv"
    )


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
