"""Tests for the executor safety checks."""

from pathlib import Path
from unittest.mock import patch

import pytest

from jellyfiler.executor import ExecutionError, _preflight, execute
from jellyfiler.models import MediaType, Plan, PlannedMove


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


# ---------------------------------------------------------------------------
# execute — dry-run
# ---------------------------------------------------------------------------

def test_execute_dry_run_does_not_move(tmp_path):
    src = tmp_path / "movie.mkv"
    dst = tmp_path / "output" / "movie.mkv"
    src.touch()
    plan = Plan(moves=[_move(src, dst)])
    execute(plan, dry_run=True)
    assert src.exists()
    assert not dst.exists()


def test_execute_empty_plan_does_nothing(tmp_path):
    execute(Plan(), dry_run=False)  # should not raise


def test_execute_dry_run_with_skipped_items(tmp_path):
    src = tmp_path / "movie.mkv"
    dst = tmp_path / "output" / "movie.mkv"
    src.touch()
    skipped = PlannedMove(
        source=tmp_path / "unknown.mkv",
        destination=tmp_path,
        media_type=MediaType.UNKNOWN,
        tmdb_id=None,
        matched_title="unknown",
        confidence="low",
        skipped=True,
        skip_reason="no title",
    )
    plan = Plan(moves=[_move(src, dst)], skipped=[skipped])
    execute(plan, dry_run=True)
    assert src.exists()


# ---------------------------------------------------------------------------
# execute — live run
# ---------------------------------------------------------------------------

def test_execute_live_moves_file(tmp_path):
    src = tmp_path / "movie.mkv"
    dst = tmp_path / "output" / "movie.mkv"
    src.touch()
    plan = Plan(moves=[_move(src, dst)])
    execute(plan, dry_run=False)
    assert not src.exists()
    assert dst.exists()


def test_execute_live_creates_destination_directories(tmp_path):
    src = tmp_path / "movie.mkv"
    dst = tmp_path / "a" / "b" / "c" / "movie.mkv"
    src.touch()
    plan = Plan(moves=[_move(src, dst)])
    execute(plan, dry_run=False)
    assert dst.exists()


def test_execute_live_preflight_failure_aborts(tmp_path):
    src = tmp_path / "movie.mkv"
    dst = tmp_path / "output" / "movie.mkv"
    src.touch()
    dst.parent.mkdir()
    dst.touch()  # destination already exists — preflight should fail
    plan = Plan(moves=[_move(src, dst)])
    with pytest.raises(ExecutionError):
        execute(plan, dry_run=False)
    assert src.exists()  # nothing was moved


def test_execute_live_records_move_in_cache(tmp_path):
    from jellyfiler.cache import Cache

    src = tmp_path / "movie.mkv"
    dst = tmp_path / "output" / "movie.mkv"
    src.touch()
    plan = Plan(moves=[_move(src, dst)])
    cache = Cache(tmp_path / "cache.db")
    execute(plan, dry_run=False, cache=cache)
    assert cache.already_moved(src)


def test_execute_live_raises_on_move_failure(tmp_path):
    src = tmp_path / "movie.mkv"
    dst = tmp_path / "output" / "movie.mkv"
    src.touch()
    plan = Plan(moves=[_move(src, dst)])
    with patch("jellyfiler.executor.shutil.move", side_effect=OSError("disk full")), pytest.raises(ExecutionError, match="failed to move"):
        execute(plan, dry_run=False)
