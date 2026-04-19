"""Scan a source directory and yield media files."""

from pathlib import Path

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".vob"}


def find_media_files(root: Path) -> list[Path]:
    """Return all video files under root, sorted by path."""
    if not root.exists():
        raise FileNotFoundError(f"Source directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Source path is not a directory: {root}")

    found = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            found.append(path)
    return found


def find_top_level_items(root: Path) -> list[Path]:
    """Return top-level files and directories under root.

    Used when the source contains one folder per movie/show rather than
    raw video files at the top level.
    """
    if not root.exists():
        raise FileNotFoundError(f"Source directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Source path is not a directory: {root}")

    return sorted(p for p in root.iterdir() if not p.name.startswith("."))
