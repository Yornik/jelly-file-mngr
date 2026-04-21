"""Tests for in-place mode — reorganize within the source directory."""

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


def test_in_place_no_conflict(tmp_path: Path) -> None:
    """In-place: flat file → subdirectory should pass preflight."""
    src = tmp_path / "Coco.2017.mkv"
    dst = tmp_path / "Coco (2017)" / "Coco (2017).mkv"
    src.touch()
    problems = _preflight([_move(src, dst)])
    assert problems == []


def test_in_place_self_move_detected(tmp_path: Path) -> None:
    """Moving a file to its own location should be caught by preflight."""
    src = tmp_path / "Coco (2017)" / "Coco (2017).mkv"
    src.parent.mkdir()
    src.touch()
    # destination == source → already exists
    problems = _preflight([_move(src, src)])
    assert any("already exists" in p for p in problems)


def test_in_place_sibling_no_conflict(tmp_path: Path) -> None:
    """Two files in same source dir going to different destinations is fine."""
    src1 = tmp_path / "Coco.2017.mkv"
    src2 = tmp_path / "Inception.2010.mkv"
    src1.touch()
    src2.touch()
    moves = [
        _move(src1, tmp_path / "Coco (2017)" / "Coco (2017).mkv"),
        _move(src2, tmp_path / "Inception (2010)" / "Inception (2010).mkv"),
    ]
    assert _preflight(moves) == []


def test_cleanup_empty_dirs(tmp_path: Path) -> None:
    """_remove_empty_dirs should only remove truly empty directories."""
    from jellyfiler.cli import _remove_empty_dirs

    # Create: empty dir + non-empty dir
    empty = tmp_path / "empty_release_folder"
    empty.mkdir()
    kept = tmp_path / "Coco (2017)"
    kept.mkdir()
    (kept / "Coco (2017).mkv").touch()

    _remove_empty_dirs(tmp_path)

    assert not empty.exists()
    assert kept.exists()
    assert (kept / "Coco (2017).mkv").exists()


def test_cleanup_does_not_remove_root(tmp_path: Path) -> None:
    """Root directory itself must never be removed."""
    from jellyfiler.cli import _remove_empty_dirs

    # All subdirs are empty — but root stays
    (tmp_path / "empty1").mkdir()
    (tmp_path / "empty2").mkdir()

    _remove_empty_dirs(tmp_path)

    assert tmp_path.exists()


def test_cleanup_returns_count(tmp_path: Path) -> None:
    """_remove_empty_dirs returns the number of directories removed."""
    from jellyfiler.cli import _remove_empty_dirs

    (tmp_path / "empty1").mkdir()
    (tmp_path / "empty2").mkdir()
    kept = tmp_path / "kept"
    kept.mkdir()
    (kept / "file.mkv").touch()

    count = _remove_empty_dirs(tmp_path)

    assert count == 2


# ── _simulate_empty_dirs ─────────────────────────────────────────────────────


def test_simulate_counts_dirs_that_would_empty(tmp_path: Path) -> None:
    """Simulation counts a dir whose only file is in the leaving set."""
    from jellyfiler.cli import _simulate_empty_dirs

    release = tmp_path / "Show.S01.720p"
    release.mkdir()
    video = release / "Show.S01E01.mkv"
    video.touch()

    count, perm_errors = _simulate_empty_dirs(tmp_path, {video})

    assert count == 1
    assert perm_errors == 0


def test_simulate_ignores_dirs_with_remaining_files(tmp_path: Path) -> None:
    """Dir with a file NOT in the leaving set is not counted."""
    from jellyfiler.cli import _simulate_empty_dirs

    release = tmp_path / "Show.S01.720p"
    release.mkdir()
    video = release / "Show.S01E01.mkv"
    nfo = release / "release.nfo"
    video.touch()
    nfo.touch()

    # Only the video leaves; nfo stays → dir is not empty
    count, perm_errors = _simulate_empty_dirs(tmp_path, {video})

    assert count == 0
    assert perm_errors == 0


def test_simulate_does_not_delete_any_files(tmp_path: Path) -> None:
    """Calling _simulate_empty_dirs must not touch the filesystem."""
    from jellyfiler.cli import _simulate_empty_dirs

    release = tmp_path / "Show.S01.720p"
    release.mkdir()
    video = release / "Show.S01E01.mkv"
    video.touch()

    _simulate_empty_dirs(tmp_path, {video})

    # File and directory must still exist after simulation
    assert video.exists()
    assert release.exists()


def test_simulate_nested_empty_dirs(tmp_path: Path) -> None:
    """Nested dirs both become empty when all their files leave."""
    from jellyfiler.cli import _simulate_empty_dirs

    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    video = inner / "file.mkv"
    video.touch()

    count, perm_errors = _simulate_empty_dirs(tmp_path, {video})

    # Both outer and inner would be removed
    assert count == 2
    assert perm_errors == 0
    # Neither actually deleted
    assert video.exists()
    assert inner.exists()
    assert outer.exists()


def test_simulate_skips_unreadable_dirs(tmp_path: Path) -> None:
    """Dirs that raise PermissionError on iterdir are treated as non-empty (safe default)."""
    from jellyfiler.cli import _simulate_empty_dirs

    locked = tmp_path / "locked_dir"
    locked.mkdir()
    video = locked / "file.mkv"
    video.touch()

    # Remove read+execute permission so iterdir raises PermissionError
    locked.chmod(0o000)
    try:
        count, perm_errors = _simulate_empty_dirs(tmp_path, {video})
        # Can't read the dir → treated as non-empty → not counted
        assert count == 0
        assert perm_errors == 1
        # File is untouched
        locked.chmod(0o755)
        assert video.exists()
    finally:
        locked.chmod(0o755)
