"""Tests for filename parsing."""

from pathlib import Path

from jellyfiler.guesser import guess
from jellyfiler.models import MediaType


def test_movie_basic():
    g = guess(Path("Blade.Runner.2049.2017.2160p.UHD.BluRay.REMUX.mkv"))
    assert g.media_type == MediaType.MOVIE
    assert "Blade Runner 2049" in g.title
    assert g.year == 2017


def test_episode_sxxexx():
    g = guess(Path("Futurama.S12E03.1080p.x265-ELiTE.mkv"))
    assert g.media_type == MediaType.EPISODE
    assert g.title == "Futurama"
    assert g.season == 12
    assert g.episode == 3


def test_episode_s13():
    g = guess(Path("Futurama.S13E06.1080p.x265-AMBER.mkv"))
    assert g.media_type == MediaType.EPISODE
    assert g.season == 13
    assert g.episode == 6


def test_movie_year_only():
    g = guess(Path("Coco.2017.2160p.UHD.BluRay.x265-WhiteRhino.mkv"))
    assert g.media_type == MediaType.MOVIE
    assert g.title == "Coco"
    assert g.year == 2017


def test_unknown_no_crash():
    g = guess(Path("some_random_file_no_pattern.mkv"))
    assert g.title is not None  # may be empty string but should not raise


def test_title_cleaning():
    g = guess(Path("The.Dark.Knight.2008.IMAX.4K.mkv"))
    assert g.title == "The Dark Knight"
    assert g.year == 2008
