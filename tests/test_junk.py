"""Tests for junk file detection and quarantine."""

from pathlib import Path

import pytest

from jellyfiler.junk import is_junk, junk_destination, move_junk

# ── is_junk: extensions ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["movie.nfo", "cover.jpg", "cover.jpeg", "hash.sfv", "info.txt", "get.url"]
)
def test_junk_by_extension(name):
    assert is_junk(Path(name))


def test_real_video_not_junk_by_extension():
    assert not is_junk(Path("movie.mkv"))
    assert not is_junk(Path("episode.mp4"))


# ── is_junk: stem patterns ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "sample.mkv",
        "Sample.mkv",
        "SAMPLE-720p.mkv",
        "movie-sample.mkv",
        "trailer.mkv",
        "theatrical-trailer.mkv",
        "teaser.mkv",
        "featurette.mkv",
        "interview.mkv",
        "behind.the.scenes.mkv",
        "deleted.scene.mkv",
        "bloopers.mkv",
        "bonus.mkv",
        "promo.mkv",
        "RARBG.com.mp4",
        "a1b2c3d4e5f6a7b8.mkv",  # hex hash
        "0123456789abcdef0123456789abcdef.mkv",  # long hex hash
    ],
)
def test_junk_by_stem(name):
    assert is_junk(Path(name))


def test_real_movie_not_junk_by_stem():
    assert not is_junk(Path("Blade.Runner.2049.2017.mkv"))
    assert not is_junk(Path("Futurama.S12E03.mkv"))


# ── is_junk: parent directory ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "parent",
    [
        "Sample",
        "Samples",
        "sample",
        "Screen",
        "Screens",
        "Screenshots",
        "Featurettes",
        "Featurette",
        "Extras",
        "Extra",
        "Bonus",
        "Trailers",
        "Trailer",
        "Behind the Scenes",
        "Deleted Scenes",
        "Deleted Scene",
        "Interviews",
        "Interview",
        "Bloopers",
        "Fake Endings",
        "Fake Ending",
        "Shorts",
        "Short",
        "Promos",
        "Specials",
    ],
)
def test_junk_by_parent_dir(tmp_path, parent):
    junk_dir = tmp_path / parent
    junk_dir.mkdir()
    f = junk_dir / "something.mkv"
    assert is_junk(f)


def test_real_file_in_normal_dir_not_junk(tmp_path):
    d = tmp_path / "Futurama Season 12"
    d.mkdir()
    f = d / "Futurama.S12E01.mkv"
    assert not is_junk(f)


def test_junk_nested_deep(tmp_path):
    """File deep inside a Featurettes folder is caught regardless of nesting."""
    nested = tmp_path / "Movie (2009)" / "Featurettes" / "The Movie" / "Fake Endings"
    nested.mkdir(parents=True)
    f = nested / "Zombie Meat.mkv"
    assert is_junk(f)


# ── junk_destination ──────────────────────────────────────────────────────────


def test_junk_destination_basic(tmp_path):
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    f = source_root / "Sample" / "sample.mkv"
    result = junk_destination(f, source_root, dest_root)
    assert result == dest_root / ".junk" / "Sample" / "sample.mkv"


def test_junk_destination_flat(tmp_path):
    source_root = tmp_path / "source"
    dest_root = tmp_path / "dest"
    f = source_root / "RARBG.com.mp4"
    result = junk_destination(f, source_root, dest_root)
    assert result == dest_root / ".junk" / "RARBG.com.mp4"


def test_junk_destination_outside_source(tmp_path):
    """If file is outside source_root, uses just the filename."""
    dest_root = tmp_path / "dest"
    f = Path("/completely/different/path/file.nfo")
    result = junk_destination(f, tmp_path / "source", dest_root)
    assert result == dest_root / ".junk" / "file.nfo"


# ── move_junk ─────────────────────────────────────────────────────────────────


def test_move_junk_moves_files(tmp_path):
    src_root = tmp_path / "source"
    dst_root = tmp_path / "dest"
    src_root.mkdir()

    f = src_root / "sample.mkv"
    f.write_text("data")

    moved, failed = move_junk([f], src_root, dst_root)
    assert moved == 1
    assert failed == 0
    assert not f.exists()
    assert (dst_root / ".junk" / "sample.mkv").exists()


def test_move_junk_skips_existing_dest(tmp_path):
    src_root = tmp_path / "source"
    dst_root = tmp_path / "dest"
    src_root.mkdir()

    f = src_root / "sample.mkv"
    f.write_text("data")

    existing = dst_root / ".junk" / "sample.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_text("already here")

    moved, failed = move_junk([f], src_root, dst_root)
    assert moved == 0
    assert failed == 1


def test_move_junk_empty_list(tmp_path):
    moved, failed = move_junk([], tmp_path / "src", tmp_path / "dst")
    assert moved == 0
    assert failed == 0
