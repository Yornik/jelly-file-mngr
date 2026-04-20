"""Detect and quarantine junk files (samples, trailers, featurettes, hash dumps)."""

import re
import shutil
from pathlib import Path

from rich.console import Console

_JUNK_DIR_NAME = ".junk"

# File extensions that are never real media content
_JUNK_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".nfo",
        ".txt",
        ".jpg",
        ".jpeg",
        ".png",
        ".bmp",
        ".gif",
        ".sfv",
        ".md5",
        ".nzb",
        ".srr",
        ".srs",
        ".url",
        ".lnk",
        ".exe",
        ".bat",
        ".com",
    }
)

# Stem patterns that indicate junk video files
_JUNK_VIDEO_STEMS = re.compile(
    r"""
    (?ix)
    ^sample                     # sample.mkv, Sample-720p.mkv
    | -sample$                  # movie-sample.mkv
    | \btrailer\b               # trailer, theatrical-trailer
    | \bteaser\b
    | \bfeaturette\b
    | \binterview\b
    | \bbehind.the.scenes\b
    | \bdeleted.scene\b
    | \bbloopers?\b
    | \bbonus\b
    | \bpromo\b
    | ^RARBG                    # RARBG.com.mp4 promo files
    | ^[0-9a-f]{16,}$           # pure hex hash filename
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Parent directory names that mark all contents as junk/extras
_JUNK_DIR_NAMES = re.compile(
    r"""
    (?ix)
    ^samples?$
    | ^screens?$
    | ^screenshots?$
    | ^featurettes?$
    | ^extras?$
    | ^bonus$
    | ^specials?$
    | ^trailers?$
    | ^behind.the.scenes$
    | ^deleted.scenes?$
    | ^interviews?$
    | ^bloopers?$
    | ^fake.endings?$
    | ^shorts?$
    | ^promos?$
    """,
    re.VERBOSE | re.IGNORECASE,
)


def is_junk(path: Path) -> bool:
    """Return True if *path* looks like a junk/sidecar file.

    Checks (in order):
    1. File extension is a known non-media type (.nfo, .jpg, etc.)
    2. Video stem matches junk patterns (sample, trailer, hex hash…)
    3. Any parent directory name matches junk folder patterns (Featurettes/, Extras/…)
    """
    suffix = path.suffix.lower()
    if suffix in _JUNK_EXTENSIONS:
        return True

    stem = path.stem
    if _JUNK_VIDEO_STEMS.search(stem):
        return True

    return any(_JUNK_DIR_NAMES.match(parent.name) for parent in path.parents)


def junk_destination(file: Path, source_root: Path, dest_root: Path) -> Path:
    """Return the quarantine path for *file* under ``dest_root/.junk/``."""
    try:
        rel = file.relative_to(source_root)
    except ValueError:
        rel = Path(file.name)
    return dest_root / _JUNK_DIR_NAME / rel


def report_junk(
    junk_files: list[Path],
    source_root: Path,
    dest_root: Path,
    dry_run: bool,
    console: Console | None = None,
) -> None:
    """Print a summary table of junk files and their quarantine destinations."""
    if not junk_files:
        return

    con = console or Console()
    verb = "Would move" if dry_run else "Moving"
    con.print(
        f"\n[bold yellow]Junk files ({len(junk_files)}) — {verb} to {dest_root / _JUNK_DIR_NAME}[/bold yellow]"
    )
    for f in junk_files:
        dest = junk_destination(f, source_root, dest_root)
        con.print(f"  [dim]{f.relative_to(source_root)}[/dim] → [dim]{dest}[/dim]")


def move_junk(
    junk_files: list[Path],
    source_root: Path,
    dest_root: Path,
) -> tuple[int, int]:
    """Move junk files to the quarantine directory. Returns (moved, failed)."""
    moved = 0
    failed = 0
    for f in junk_files:
        dest = junk_destination(f, source_root, dest_root)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                failed += 1
                continue
            shutil.move(str(f), dest)
            moved += 1
        except OSError:
            failed += 1
    return moved, failed
