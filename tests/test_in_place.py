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
