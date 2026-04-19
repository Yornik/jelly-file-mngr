"""Tests for the executor safety checks."""

from pathlib import Path

from jellyfiler.executor import _preflight
from jellyfiler.models import MediaType, PlannedMove


def _move(source: Path, dest: Path) -> PlannedMove:
    return PlannedMove(
        source=source,
        destination=dest,
        media_type=MediaType.MOVIE,
        tmdb_id=1,
        matched_title="Test",
        confidence="high",
    )


def test_preflight_missing_source(tmp_path):
    move = _move(tmp_path / "nonexistent.mkv", tmp_path / "dest/movie.mkv")
    problems = _preflight([move])
    assert any("no longer exists" in p for p in problems)


def test_preflight_destination_exists(tmp_path):
    src = tmp_path / "source.mkv"
    dst = tmp_path / "dest.mkv"
    src.touch()
    dst.touch()
    move = _move(src, dst)
    problems = _preflight([move])
    assert any("already exists" in p for p in problems)


def test_preflight_duplicate_destination(tmp_path):
    src1 = tmp_path / "a.mkv"
    src2 = tmp_path / "b.mkv"
    dst = tmp_path / "output" / "same.mkv"
    src1.touch()
    src2.touch()
    problems = _preflight([_move(src1, dst), _move(src2, dst)])
    assert any("Duplicate" in p for p in problems)


def test_preflight_clean(tmp_path):
    src = tmp_path / "movie.mkv"
    dst = tmp_path / "output" / "movie.mkv"
    src.touch()
    problems = _preflight([_move(src, dst)])
    assert problems == []
