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


def test_execute_prints_skipped_list_even_when_no_moves(tmp_path):
    """Bug fix: when plan.moves is empty but plan.skipped has items, the skipped
    list must still be printed (otherwise the user has no idea why nothing happened)."""
    skipped_only = Plan(
        moves=[],
        skipped=[
            PlannedMove(
                source=tmp_path / "weird.mkv",
                destination=tmp_path / "out",
                media_type=MediaType.EPISODE,
                tmdb_id=None,
                matched_title="weird",
                confidence="low",
                skipped=True,
                skip_reason="Ambiguous: 5 results, no confident match",
            ),
            PlannedMove(
                source=tmp_path / "other.mkv",
                destination=tmp_path / "out",
                media_type=MediaType.EPISODE,
                tmdb_id=None,
                matched_title="other",
                confidence="low",
                skipped=True,
                skip_reason="No TMDB match",
            ),
        ],
    )
    # Capture rich console output by redirecting the module-level console
    from io import StringIO

    from rich.console import Console

    import jellyfiler.executor as exec_module

    buf = StringIO()
    original = exec_module.console
    exec_module.console = Console(file=buf, force_terminal=False, width=120)
    try:
        execute(skipped_only, dry_run=False)
    finally:
        exec_module.console = original

    output = buf.getvalue()
    # The skipped-list bullet section must be present
    assert "Skipped (2)" in output
    assert "weird.mkv" in output
    assert "Ambiguous" in output
    assert "other.mkv" in output


def test_execute_truncates_huge_plans_by_default(tmp_path):
    """A 100-move plan with full_plan=False shows only 50 rows + a footer."""
    moves = [
        PlannedMove(
            source=tmp_path / f"file{i:03d}.mkv",
            destination=tmp_path / "out" / f"S01E{i:02d}.mkv",
            media_type=MediaType.EPISODE,
            tmdb_id=1,
            matched_title="Show",
            confidence="high",
        )
        for i in range(100)
    ]
    plan = Plan(moves=moves)

    from io import StringIO

    from rich.console import Console

    import jellyfiler.executor as exec_module

    buf = StringIO()
    original = exec_module.console
    exec_module.console = Console(file=buf, force_terminal=False, width=200)
    try:
        # Dry run so we don't try to actually move anything
        execute(plan, dry_run=True, full_plan=False)
    finally:
        exec_module.console = original

    output = buf.getvalue()
    # Footer indicating truncation
    assert "and 50 more" in output  # 100 - 50 limit = 50 hidden


def test_execute_full_plan_shows_everything(tmp_path):
    moves = [
        PlannedMove(
            source=tmp_path / f"file{i:03d}.mkv",
            destination=tmp_path / "out" / f"S01E{i:02d}.mkv",
            media_type=MediaType.EPISODE,
            tmdb_id=1,
            matched_title="Show",
            confidence="high",
        )
        for i in range(100)
    ]
    plan = Plan(moves=moves)

    from io import StringIO

    from rich.console import Console

    import jellyfiler.executor as exec_module

    buf = StringIO()
    original = exec_module.console
    exec_module.console = Console(file=buf, force_terminal=False, width=200)
    try:
        execute(plan, dry_run=True, full_plan=True)
    finally:
        exec_module.console = original

    output = buf.getvalue()
    # No truncation footer
    assert "more move" not in output
    # All filenames present (sample-check first and last)
    assert "file000.mkv" in output
    assert "file099.mkv" in output


def test_execute_truncates_skipped_list_too(tmp_path):
    skipped = [
        PlannedMove(
            source=tmp_path / f"skip{i:03d}.mkv",
            destination=tmp_path / "out",
            media_type=MediaType.EPISODE,
            tmdb_id=None,
            matched_title="x",
            confidence="low",
            skipped=True,
            skip_reason="reason",
        )
        for i in range(75)
    ]
    plan = Plan(moves=[], skipped=skipped)

    from io import StringIO

    from rich.console import Console

    import jellyfiler.executor as exec_module

    buf = StringIO()
    original = exec_module.console
    exec_module.console = Console(file=buf, force_terminal=False, width=200)
    try:
        execute(plan, dry_run=False, full_plan=False)
    finally:
        exec_module.console = original

    output = buf.getvalue()
    assert "Skipped (75)" in output
    assert "and 25 more" in output  # 75 - 50 = 25 hidden


def test_execute_truly_empty_plan_says_nothing_to_do(tmp_path):
    plan = Plan(moves=[], skipped=[])
    from io import StringIO

    from rich.console import Console

    import jellyfiler.executor as exec_module

    buf = StringIO()
    original = exec_module.console
    exec_module.console = Console(file=buf, force_terminal=False, width=120)
    try:
        execute(plan, dry_run=False)
    finally:
        exec_module.console = original
    output = buf.getvalue()
    assert "Nothing to do" in output


def test_execute_live_raises_on_move_failure(tmp_path):
    src = tmp_path / "movie.mkv"
    dst = tmp_path / "output" / "movie.mkv"
    src.touch()
    plan = Plan(moves=[_move(src, dst)])
    with (
        patch("jellyfiler.executor.shutil.move", side_effect=OSError("disk full")),
        pytest.raises(ExecutionError, match="failed to move"),
    ):
        execute(plan, dry_run=False)


# ---------------------------------------------------------------------------
# _short_dest — compact path display
# ---------------------------------------------------------------------------


def test_short_dest_three_parts_keeps_last_three():
    from jellyfiler.executor import _short_dest

    dest = Path("/big/long/root/Show/Season 01/S01E01.mkv")
    out = _short_dest(dest, None)
    assert out.endswith("Show/Season 01/S01E01.mkv")


def test_short_dest_two_parts_returns_both():
    from jellyfiler.executor import _short_dest

    out = _short_dest(Path("Movie/file.mkv"), None)
    assert out == "Movie/file.mkv"


def test_short_dest_single_part_returns_name():
    from jellyfiler.executor import _short_dest

    out = _short_dest(Path("file.mkv"), None)
    assert out == "file.mkv"


# ---------------------------------------------------------------------------
# Subtitle move failure path during live execute
# ---------------------------------------------------------------------------


def test_execute_logs_subtitle_failure_but_video_still_moves(tmp_path):
    """If a subtitle's _move_subtitle raises, the video move still succeeds."""
    src = tmp_path / "ep.mkv"
    sub = tmp_path / "ep.srt"
    src.touch()
    sub.touch()
    dst = tmp_path / "out" / "ep.mkv"
    plan = Plan(moves=[_move(src, dst)])

    with patch("jellyfiler.executor._move_subtitle", side_effect=OSError("sub move failed")):
        execute(plan, dry_run=False)

    assert dst.exists()  # video moved
    assert sub.exists()  # subtitle untouched (failed to move)
