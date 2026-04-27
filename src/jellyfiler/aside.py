"""Identify and route non-canonical content (extras, samples, sidecars).

The previous incarnation of this module was called ``junk``. It dumped every
non-canonical file into ``dest/.junk/`` so the user wouldn't lose data, but
that flattened useful content (DVD extras, behind-the-scenes, deleted scenes)
into a single discard pile that Jellyfin would never pick up.

This module classifies non-canonical files into typed buckets so the CLI can:

  * Route ``EXTRAS``, ``FEATURETTES``, ``BEHIND_THE_SCENES``, ``DELETED_SCENES``,
    ``INTERVIEWS``, ``TRAILERS``, ``SHORTS``, ``BLOOPERS`` into the matching
    Jellyfin-recognised subdirectory underneath the parent media item's
    destination — Jellyfin then displays them as bonus content.
  * Send ``DISCARD`` files (samples, NCOP/NCED, hash-named, sidecar .nfo etc.)
    to ``dest/.aside/`` for safe quarantine, or PERMANENTLY DELETE them with
    the ``--remove-discards --i-mean-it`` opt-in flag pair.

See :func:`classify_aside` for the classification rules and
:data:`JELLYFIN_EXTRAS_SUBDIR` for the Jellyfin subdir mapping.
"""

import re
import shutil
from enum import StrEnum
from pathlib import Path

from rich.console import Console

console = Console()


class AsideKind(StrEnum):
    """Type of non-canonical content for routing decisions."""

    DISCARD = "discard"  # samples, hash-named, sidecar files — no value
    EXTRAS = "extras"  # generic "Extras" / "Bonus" / "DVD Extras"
    FEATURETTES = "featurettes"
    BEHIND_THE_SCENES = "behind_the_scenes"
    DELETED_SCENES = "deleted_scenes"
    INTERVIEWS = "interviews"
    TRAILERS = "trailers"
    SHORTS = "shorts"
    BLOOPERS = "bloopers"  # no Jellyfin-specific bucket → routes to extras/
    # Anime non-credit Opening / Ending tracks. Not a Jellyfin-defined bucket
    # but clearly bonus content worth preserving — routed to extras/op-ed/ so
    # it shows up under the Jellyfin Extras tab grouped together.
    ANIME_OP_ED = "anime_op_ed"


# Map our category to the Jellyfin-recognised subdirectory name. Anything not
# in this dict (currently only DISCARD) goes to ``.aside/`` or is unlinked.
JELLYFIN_EXTRAS_SUBDIR: dict[AsideKind, str] = {
    AsideKind.EXTRAS: "extras",
    AsideKind.FEATURETTES: "featurettes",
    AsideKind.BEHIND_THE_SCENES: "behind the scenes",
    AsideKind.DELETED_SCENES: "deleted scenes",
    AsideKind.INTERVIEWS: "interviews",
    AsideKind.TRAILERS: "trailers",
    AsideKind.SHORTS: "shorts",
    AsideKind.BLOOPERS: "extras",  # Jellyfin has no bloopers bucket — fold into extras
    # OP/ED tracks live two levels deep so the show folder gets:
    #   Show/extras/        ← Jellyfin Extras tab
    #   Show/extras/op-ed/  ← OP/ED files grouped together
    AsideKind.ANIME_OP_ED: "extras/op-ed",
}


# ───────────────────────────────────────────────────────────────────────────
# Detection regexes
# ───────────────────────────────────────────────────────────────────────────

# Video stems that scream "this is not a real episode/movie": sample/trailer/
# release-group promo. Always DISCARD.
_DISCARD_VIDEO_STEMS = re.compile(
    r"^("
    r"sample"
    r"|rarbg[\. ]?(com|info)?"
    r"|etrg"
    r"|www\."
    r")\b",
    re.IGNORECASE,
)

# Anime non-credit OP/ED tracks. Always DISCARD (intro/outro music videos).
_ANIME_OP_ED = re.compile(
    r"(?:^|[\W_])"
    r"(?:NCOP|NCED"
    r"|Creditless[\W_]+(?:OP|ED|Opening|Ending)"
    r"|Non[\W_]?Credit[\W_]+(?:OP|ED|Opening|Ending))"
    r"\d*"
    r"(?:$|[\W_])",
    re.IGNORECASE,
)

# A file whose entire stem is a long hex hash (MD5/SHA-like) is DISCARD.
_HEX_HASH = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)

# Non-video sidecar extensions torrent releases drop. Always DISCARD.
_DISCARD_EXTENSIONS = {
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

# Video extensions — only these get the stem-based DISCARD checks.
_VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".vob"}

# Directory name → AsideKind. Case-insensitive whole-name match.
# When a parent directory name matches one of these, every file inside gets the
# corresponding kind (unless overridden by a stronger DISCARD signal).
_DIR_NAME_KIND: list[tuple[re.Pattern[str], AsideKind]] = [
    # Always DISCARD regardless of where they live
    (re.compile(r"^samples?$", re.IGNORECASE), AsideKind.DISCARD),
    (re.compile(r"^screens?$|^screenshots?$", re.IGNORECASE), AsideKind.DISCARD),
    (re.compile(r"^Promos?$", re.IGNORECASE), AsideKind.DISCARD),
    # Anime OP/ED folder names — preserved as bonus content under the show
    (re.compile(r"^OPs?$|^Openings?$", re.IGNORECASE), AsideKind.ANIME_OP_ED),
    (re.compile(r"^EDs?$|^Endings?$", re.IGNORECASE), AsideKind.ANIME_OP_ED),
    (re.compile(r"^Creditless$", re.IGNORECASE), AsideKind.ANIME_OP_ED),
    (re.compile(r"^NC(OP|ED)s?$", re.IGNORECASE), AsideKind.ANIME_OP_ED),
    # Jellyfin-recognised extras buckets — route into the matching dest subdir
    (re.compile(r"^featurettes?$", re.IGNORECASE), AsideKind.FEATURETTES),
    (re.compile(r"^behind\ the\ scenes$", re.IGNORECASE), AsideKind.BEHIND_THE_SCENES),
    (re.compile(r"^deleted\ scenes?$", re.IGNORECASE), AsideKind.DELETED_SCENES),
    (re.compile(r"^interviews?$", re.IGNORECASE), AsideKind.INTERVIEWS),
    (re.compile(r"^trailers?$", re.IGNORECASE), AsideKind.TRAILERS),
    (re.compile(r"^shorts?$", re.IGNORECASE), AsideKind.SHORTS),
    (re.compile(r"^bloopers?$", re.IGNORECASE), AsideKind.BLOOPERS),
    (re.compile(r"^fake\ endings?$", re.IGNORECASE), AsideKind.EXTRAS),
    # Generic "extras" buckets — DVD Extras, Bonus, Bonus Features, Specials
    (re.compile(r"^extras?$", re.IGNORECASE), AsideKind.EXTRAS),
    (re.compile(r"^DVD[\s_-]?Extras?$", re.IGNORECASE), AsideKind.EXTRAS),
    (re.compile(r"^Bonus(\s+Features?)?$", re.IGNORECASE), AsideKind.EXTRAS),
    (re.compile(r"^Features?$", re.IGNORECASE), AsideKind.EXTRAS),
    (re.compile(r"^Specials?$", re.IGNORECASE), AsideKind.EXTRAS),
]

_ASIDE_DIR_NAME = ".aside"  # the discard pile (was previously .junk)


# ───────────────────────────────────────────────────────────────────────────
# Classification
# ───────────────────────────────────────────────────────────────────────────


def classify_aside(path: Path) -> AsideKind | None:
    """Return the kind of non-canonical content if path is an aside, else None.

    Rule order (first match wins):

    1. Non-video sidecar extensions (``.nfo``, ``.txt``, ``.jpg`` …) → ``DISCARD``
    2. Video stem matches a discard pattern (``sample``, ``rarbg`` …) → ``DISCARD``
    3. Video stem is a long hex hash → ``DISCARD``
    4. Video stem matches an anime NCOP/NCED pattern → ``DISCARD``
    5. Any ancestor directory name matches a known kind → that kind
    6. Otherwise → ``None`` (i.e., real media, leave it to the main flow)
    """
    suffix = path.suffix.lower()
    stem = path.stem

    if suffix in _DISCARD_EXTENSIONS:
        return AsideKind.DISCARD

    if suffix in _VIDEO_EXTENSIONS:
        if _DISCARD_VIDEO_STEMS.match(stem):
            return AsideKind.DISCARD
        if _HEX_HASH.match(stem):
            return AsideKind.DISCARD
        # Anime non-credit OP/ED tracks: bonus content, not garbage.
        # Routed to extras/op-ed/ rather than DISCARD.
        if _ANIME_OP_ED.search(stem):
            return AsideKind.ANIME_OP_ED

    # Walk ancestors looking for a known dir-name pattern.
    for parent in path.parents:
        for pattern, kind in _DIR_NAME_KIND:
            if pattern.match(parent.name):
                return kind

    return None


def is_aside(path: Path) -> bool:
    """Convenience wrapper: True iff classify_aside(path) is not None."""
    return classify_aside(path) is not None


# ───────────────────────────────────────────────────────────────────────────
# Bulk operations
# ───────────────────────────────────────────────────────────────────────────


def find_aside(root: Path) -> list[tuple[Path, AsideKind]]:
    """Return all aside files under ``root`` along with their kinds, sorted by path."""
    out: list[tuple[Path, AsideKind]] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        kind = classify_aside(p)
        if kind is not None:
            out.append((p, kind))
    return out


def aside_destination(file: Path, source_root: Path, dest_root: Path) -> Path:
    """Where ``file`` lands in the discard quarantine pile.

    Structure: ``dest_root/.aside/<relative-subdir-from-source-root>/<filename>``.
    """
    try:
        rel = file.relative_to(source_root)
    except ValueError:
        rel = Path(file.name)
    return dest_root / _ASIDE_DIR_NAME / rel


def report_aside(
    aside_files: list[Path], source_root: Path, dest_root: Path, dry_run: bool
) -> None:
    """Print a summary of aside files and where they would land."""
    if not aside_files:
        console.print("[dim]No aside files found.[/dim]")
        return

    _LIMIT = 10
    action = "Would move" if dry_run else "Moving"
    console.print(
        f"\n[bold yellow]Aside files ({len(aside_files)}) — {action} to {dest_root / _ASIDE_DIR_NAME}:[/bold yellow]"
    )
    for f in aside_files[:_LIMIT]:
        dest = aside_destination(f, source_root, dest_root)
        console.print(f"  [yellow]→[/yellow] {f.name}  [dim]→ {dest}[/dim]")
    if len(aside_files) > _LIMIT:
        console.print(f"  [dim]… and {len(aside_files) - _LIMIT} more[/dim]")


def move_aside(aside_files: list[Path], source_root: Path, dest_root: Path) -> tuple[int, int]:
    """Move aside files into ``dest_root/.aside`` preserving relative structure.

    Returns ``(moved, failed)`` counts.
    """
    moved = failed = 0
    for f in aside_files:
        dest = aside_destination(f, source_root, dest_root)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), dest)
            console.print(f"  [green]✓ moved:[/green] {f.name}  [dim]→ {dest}[/dim]")
            moved += 1
        except OSError as exc:
            console.print(f"  [red]✗ failed:[/red] {f} — {exc}")
            failed += 1
    return moved, failed
