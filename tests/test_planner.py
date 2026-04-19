"""Tests for move planning logic."""

from pathlib import Path

from jellyfiler.models import GuessedMedia, MediaType, TmdbMatch
from jellyfiler.planner import _movie_destination, _episode_destination, _safe_name


def test_safe_name_strips_bad_chars():
    assert _safe_name('Movie: "The" <End>') == "Movie  The  End"


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
