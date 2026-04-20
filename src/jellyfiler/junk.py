"""Identify and quarantine junk files left by torrent releases.

Junk files are files that are clearly not the main media content:
- RARBG/ETRG/scene promo videos
- Sample and trailer files
- Hash-named files (e.g. 8fa41b40995c44c9a883b1e0fe62f16a.mkv)
- Non-media files dropped by torrent clients (.nfo, .txt, .sfv, etc.)

In dry-run mode junk files are reported without being moved.
With --apply they are moved to dest/_Junk/<relative-path-from-source>/filename.
"""

import re
import shutil
from pathlib import Path

from rich.console import Console

console = Console()

# Video files whose names match these patterns are considered junk.
# Match against the stem (filename without extension), case-insensitive.
_JUNK_VIDEO_STEMS = re.compile(
    r"^("
    r"sample"
    r"|trailer"
    r"|rarbg[\. ]?(com|info)?"
    r"|etrg"
    r"|www\."
    r"|featurette"
    r"|deleted[\. _-]?scenes?"
    r"|behind[\. _-]?the[\. _-]?scenes?"
    r"|interview"
    r"|short[\. _-]?film"
    r"|scene"
    r")\b",
    re.IGNORECASE,
)

# A file whose entire stem is a hex hash (MD5/SHA-like) is junk.
_HEX_HASH = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)

# Non-video extensions that torrent releases drop alongside the main file.
_JUNK_EXTENSIONS = {
    ".nfo",
    ".txt",
    ".sfv",
    ".md5",
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".gif",
    ".sub",
    ".idx",
    ".srr",
    ".url",
    ".htm",
    ".html",
}

# Video extensions — only these are checked against the name patterns.
_VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".vob"}

_JUNK_DIR_NAME = ".junk"


def is_junk(path: Path) -> bool:
    """Return True if path is almost certainly a junk file."""
    suffix = path.suffix.lower()
    stem = path.stem

    # Non-video sidecar files are always junk
    if suffix in _JUNK_EXTENSIONS:
        return True

    # Video files: check name against known junk patterns
    if suffix in _VIDEO_EXTENSIONS:
        if _JUNK_VIDEO_STEMS.match(stem):
            return True
        if _HEX_HASH.match(stem):
            return True

    return False


def find_junk(root: Path) -> list[Path]:
    """Return all junk files found recursively under root, sorted."""
    return sorted(p for p in root.rglob("*") if p.is_file() and is_junk(p))


def junk_destination(file: Path, source_root: Path, dest_root: Path) -> Path:
    """Return the path where *file* would land in the _Junk quarantine dir.

    Structure: dest_root/_Junk/<relative-subdir-from-source-root>/filename
    """
    try:
        rel = file.relative_to(source_root)
    except ValueError:
        rel = Path(file.name)
    return dest_root / _JUNK_DIR_NAME / rel


def report_junk(junk_files: list[Path], source_root: Path, dest_root: Path, dry_run: bool) -> None:
    """Print a summary of junk found and where files would be (or are being) moved."""
    if not junk_files:
        console.print("[dim]No junk files found.[/dim]")
        return

    action = "Would move" if dry_run else "Moving"
    console.print(
        f"\n[bold yellow]Junk files ({len(junk_files)}) — {action} to {dest_root / _JUNK_DIR_NAME}:[/bold yellow]"
    )
    for f in junk_files:
        dest = junk_destination(f, source_root, dest_root)
        console.print(f"  [yellow]→[/yellow] {f.name}  [dim]→ {dest}[/dim]")


def move_junk(junk_files: list[Path], source_root: Path, dest_root: Path) -> tuple[int, int]:
    """Move junk files into dest_root/_Junk preserving relative structure.

    Returns (moved, failed) counts.
    """
    moved = failed = 0
    for f in junk_files:
        dest = junk_destination(f, source_root, dest_root)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), dest)
            console.print(f"  [green]✓ moved:[/green] {f.name}  [dim]→ {dest}[/dim]")
            moved += 1
        except OSError as exc:
            console.print(f"  [red]✗ failed:[/red] {f} — {exc}")
            failed += 1
    return moved, failed
