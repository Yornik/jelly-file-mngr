"""Tests for media file scanning."""


import pytest

from jellyfiler.scanner import find_media_files, find_top_level_items


def test_find_media_files_returns_video_files(tmp_path):
    (tmp_path / "movie.mkv").touch()
    (tmp_path / "show.mp4").touch()
    (tmp_path / "readme.txt").touch()
    (tmp_path / "cover.jpg").touch()
    names = {p.name for p in find_media_files(tmp_path)}
    assert names == {"movie.mkv", "show.mp4"}


@pytest.mark.parametrize("ext", [".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".vob"])
def test_find_media_files_all_extensions(tmp_path, ext):
    (tmp_path / f"file{ext}").touch()
    result = find_media_files(tmp_path)
    assert len(result) == 1


def test_find_media_files_extension_case_insensitive(tmp_path):
    (tmp_path / "FILE.MKV").touch()
    assert len(find_media_files(tmp_path)) == 1


def test_find_media_files_recurses_subdirectories(tmp_path):
    subdir = tmp_path / "Show" / "Season 01"
    subdir.mkdir(parents=True)
    (subdir / "S01E01.mkv").touch()
    result = find_media_files(tmp_path)
    assert len(result) == 1
    assert result[0].name == "S01E01.mkv"


def test_find_media_files_returns_sorted(tmp_path):
    (tmp_path / "zzz.mkv").touch()
    (tmp_path / "aaa.mkv").touch()
    (tmp_path / "mmm.mkv").touch()
    result = find_media_files(tmp_path)
    assert result == sorted(result)


def test_find_media_files_empty_directory(tmp_path):
    assert find_media_files(tmp_path) == []


def test_find_media_files_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_media_files(tmp_path / "nonexistent")


def test_find_media_files_not_a_directory(tmp_path):
    f = tmp_path / "movie.mkv"
    f.touch()
    with pytest.raises(NotADirectoryError):
        find_media_files(f)


def test_find_top_level_items_returns_files_and_dirs(tmp_path):
    (tmp_path / "MovieA").mkdir()
    (tmp_path / "file.mkv").touch()
    names = {p.name for p in find_top_level_items(tmp_path)}
    assert names == {"MovieA", "file.mkv"}


def test_find_top_level_items_excludes_dot_prefixed(tmp_path):
    (tmp_path / ".junk").mkdir()
    (tmp_path / ".hidden").touch()
    (tmp_path / "Visible").mkdir()
    names = {p.name for p in find_top_level_items(tmp_path)}
    assert names == {"Visible"}


def test_find_top_level_items_does_not_recurse(tmp_path):
    subdir = tmp_path / "Show"
    subdir.mkdir()
    (subdir / "S01E01.mkv").touch()
    result = find_top_level_items(tmp_path)
    assert len(result) == 1
    assert result[0].name == "Show"


def test_find_top_level_items_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_top_level_items(tmp_path / "nonexistent")


def test_find_top_level_items_not_a_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.touch()
    with pytest.raises(NotADirectoryError):
        find_top_level_items(f)
