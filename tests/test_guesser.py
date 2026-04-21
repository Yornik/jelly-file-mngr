"""Tests for filename parsing."""

from pathlib import Path
from unittest.mock import patch

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


def test_unknown_type():
    # Force guessit to return an unrecognised type
    with patch("jellyfiler.guesser.guessit.guessit", return_value={"type": "other", "title": "X"}):
        g = guess(Path("X.mkv"))
    assert g.media_type == MediaType.UNKNOWN


def test_list_title_uses_first_element():
    with patch(
        "jellyfiler.guesser.guessit.guessit",
        return_value={"type": "movie", "title": ["First", "Second"]},
    ):
        g = guess(Path("X.mkv"))
    assert g.title == "First"


def test_list_year_uses_first_element():
    with patch(
        "jellyfiler.guesser.guessit.guessit",
        return_value={"type": "movie", "title": "Movie", "year": [2020, 2021]},
    ):
        g = guess(Path("X.mkv"))
    assert g.year == 2020


def test_list_season_uses_first_element():
    with patch(
        "jellyfiler.guesser.guessit.guessit",
        return_value={"type": "episode", "title": "Show", "season": [1, 2], "episode": 1},
    ):
        g = guess(Path("X.mkv"))
    assert g.season == 1


def test_list_episode_uses_min_as_start():
    with patch(
        "jellyfiler.guesser.guessit.guessit",
        return_value={"type": "episode", "title": "Show", "season": 1, "episode": [3, 4]},
    ):
        g = guess(Path("X.mkv"))
    assert g.episode == 3
    assert g.episode_end == 4


def test_multi_episode_range():
    """S03E01E02E03 → episode=1, episode_end=3."""
    with patch(
        "jellyfiler.guesser.guessit.guessit",
        return_value={"type": "episode", "title": "Show", "season": 3, "episode": [1, 2, 3]},
    ):
        g = guess(Path("Show.S03E01E02E03.mkv"))
    assert g.episode == 1
    assert g.episode_end == 3


def test_single_episode_has_no_episode_end():
    g = guess(Path("Futurama.S12E03.1080p.x265-ELiTE.mkv"))
    assert g.episode == 3
    assert g.episode_end is None


def test_title_from_parent_dir():
    """Bare episode filename gets title from the season-pack parent folder."""
    p = Path("Futurama.S12.1080p.x265-ELiTE") / "S12E01.mkv"
    g = guess(p)
    assert g.title == "Futurama"
    assert g.episode == 1


def test_season_from_parent_dir():
    """Season number falls back to parent dir when the filename omits it."""
    p = Path("Futurama.S12.1080p.x265-ELiTE") / "E03.mkv"
    g = guess(p)
    assert g.season == 12


def test_year_from_parent_dir():
    """Year falls back to parent dir when the filename omits it."""
    p = Path("Blade.Runner.2049.2017.UHD") / "Blade.Runner.2049.mkv"
    g = guess(p)
    assert g.year == 2017


def test_show_title_from_grandparent_season_folder(tmp_path: Path) -> None:
    """Show/Season N/bare-episode.mp4 — show title comes from grandparent, not episode name."""
    d = tmp_path / "Phineas and Ferb" / "Season 02"
    d.mkdir(parents=True)
    f = d / "Hail Doofania!.mp4"
    f.touch()
    g = guess(f)
    assert g.title == "Phineas and Ferb"
    assert g.season == 2
    assert g.media_type == MediaType.EPISODE


def test_numeric_prefix_episode_with_grandparent(tmp_path: Path) -> None:
    """082 - What Do It Do.mp4 in Show/Season 02/ → title=show, episode=82."""
    d = tmp_path / "Phineas and Ferb" / "Season 02"
    d.mkdir(parents=True)
    f = d / "082 - What Do It Do.mp4"
    f.touch()
    g = guess(f)
    assert g.title == "Phineas and Ferb"
    assert g.season == 2
    assert g.episode == 82
    assert g.media_type == MediaType.EPISODE


def test_well_named_episode_not_overridden_by_grandparent(tmp_path: Path) -> None:
    """S01E01 file already has the right show title — grandparent must not override it."""
    d = tmp_path / "Some Other Folder" / "Season 01"
    d.mkdir(parents=True)
    f = d / "Futurama.S01E01.mkv"
    f.touch()
    g = guess(f)
    # Futurama comes from the filename; "Some Other Folder" must not win
    assert g.title == "Futurama"
