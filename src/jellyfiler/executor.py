"""Execute a plan by safely moving files.

Data safety rules:
- Never delete anything.
- Never overwrite an existing destination.
- Create destination directories only when moving, never pre-emptively.
- Abort the entire operation if any pre-flight check fails.
- All moves are logged before execution.
"""

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from jellyfiler.models import MediaType, Plan, PlannedMove
from jellyfiler.scanner import SUBTITLE_EXTENSIONS

# ISO 639-1/639-2 language code: 2-3 lowercase letters
_LANG_CODE = re.compile(r"^[a-z]{2,3}$")

if TYPE_CHECKING:
    from jellyfiler.cache import Cache

console = Console()

_TYPE_ICON = {
    MediaType.MOVIE: "🎬",
    MediaType.EPISODE: "📺",
    MediaType.UNKNOWN: "?",
}


class ExecutionError(Exception):
    """Raised when a safety check fails before or during execution."""


def _subtitle_companions(source: Path) -> list[Path]:
    """Return subtitle files that share source's stem.

    Searches the source's own directory plus any immediate subdirectories
    (e.g. Subs/, Subtitles/, English/) to handle the common torrent pattern
    where subtitle files are packed in a sub-folder next to the video.

    Matches exact stem (``episode.srt``) and stem-plus-lang-code (``episode.en.srt``).
    """
    stem = source.stem
    companions = []

    search_dirs = [source.parent] + [
        d for d in source.parent.iterdir() if d.is_dir() and d != source.parent
    ]

    for search_dir in search_dirs:
        for candidate in search_dir.iterdir():
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            # exact match: episode.srt
            if candidate.stem == stem:
                companions.append(candidate)
                continue
            # lang-code match: episode.en.srt → inner_stem="episode", lang="en"
            inner = Path(candidate.stem)
            if inner.stem == stem and _LANG_CODE.match(inner.suffix.lstrip(".")):
                companions.append(candidate)
    return companions


def _move_subtitle(sub: Path, dest_video: Path) -> None:
    """Move a subtitle file next to dest_video, renaming it to match the video stem."""
    video_stem = dest_video.stem
    # Preserve language code if present: episode.en.srt → lang_suffix=".en"
    inner = Path(sub.stem)
    lang_suffix = inner.suffix if _LANG_CODE.match(inner.suffix.lstrip(".")) else ""
    sub_dest = dest_video.parent / f"{video_stem}{lang_suffix}{sub.suffix.lower()}"
    if sub_dest.exists():
        console.print(f"[dim]  subtitle already exists, skipping: {sub_dest.name}[/dim]")
        return
    sub_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(sub), str(sub_dest))
    console.print(f"[dim]  subtitle:[/dim] {sub.name}  →  {sub_dest.name}")


def _preflight(moves: list[PlannedMove]) -> list[str]:
    """Return a list of problems found before touching the filesystem."""
    problems = []
    seen_destinations: set[Path] = set()

    for move in moves:
        if not move.source.exists():
            problems.append(f"Source no longer exists: {move.source}")

        if move.destination.exists():
            problems.append(f"Destination already exists (would overwrite): {move.destination}")

        if move.destination in seen_destinations:
            problems.append(
                f"Duplicate destination in plan — two files would land at: {move.destination}"
            )
        seen_destinations.add(move.destination)

    return problems


def execute(
    plan: Plan,
    dry_run: bool = True,
    cache: "Cache | None" = None,
    source_root: Path | None = None,
    full_plan: bool = False,
) -> None:
    """Execute the plan.

    In dry-run mode (default) nothing is touched — the plan is printed only.
    Pass dry_run=False to actually move files.

    Aborts before touching anything if any pre-flight check fails.

    When ``full_plan=False`` (default), the plan table truncates large move/skip
    lists to keep terminal output usable on libraries with thousands of files.
    Pass ``full_plan=True`` (CLI: ``--full-plan``) to dump the lot.
    """
    # Even when there's nothing to MOVE, the user still cares about the
    # skipped list ("why didn't anything happen?") — print it before returning.
    if not plan.moves and not plan.skipped:
        console.print("[yellow]Nothing to do — no media files to plan or skip.[/yellow]")
        return
    if not plan.moves:
        console.print("[yellow]Nothing to move.[/yellow]")
        _print_plan(plan, source_root, full_plan=full_plan)
        return

    if dry_run:
        console.print("\n[bold cyan]DRY RUN — no files will be moved[/bold cyan]\n")
    else:
        console.print("\n[bold red]LIVE RUN — files will be moved[/bold red]\n")

    _print_plan(plan, source_root, full_plan=full_plan)

    if dry_run:
        console.print(
            f"\n[bold]Dry run complete.[/bold] {len(plan.moves)} moves planned, "
            f"{len(plan.skipped)} skipped. Pass --apply to execute."
        )
        return

    # Pre-flight: check everything before touching a single file
    problems = _preflight(plan.moves)
    if problems:
        console.print("\n[bold red]Pre-flight checks failed — aborting, nothing moved:[/bold red]")
        for p in problems:
            console.print(f"  [red]✗[/red] {p}")
        raise ExecutionError(
            f"Aborted: {len(problems)} pre-flight check(s) failed. No files were moved."
        )

    console.print("\n[bold]Pre-flight checks passed. Starting moves...[/bold]\n")

    moved = 0
    failed = 0

    for move in plan.moves:
        try:
            move.destination.parent.mkdir(parents=True, exist_ok=True)

            # Final safety check immediately before moving
            if move.destination.exists():
                console.print(f"[red]SKIP (appeared since preflight): {move.destination}[/red]")
                failed += 1
                continue

            subs = _subtitle_companions(move.source)
            shutil.move(str(move.source), str(move.destination))
            console.print(f"[green]✓[/green] {move.source.name}  →  {move.destination}")
            if cache is not None:
                cache.record_move(move.source, move.destination)
            moved += 1
            for sub in subs:
                try:
                    _move_subtitle(sub, move.destination)
                except Exception as exc:
                    console.print(f"[yellow]  subtitle move failed:[/yellow] {sub.name} — {exc}")

        except Exception as exc:
            console.print(f"[red]✗ FAILED:[/red] {move.source.name} — {exc}")
            failed += 1

    console.print(
        f"\n[bold]Done.[/bold] {moved} moved, {failed} failed, {len(plan.skipped)} skipped."
    )
    if failed:
        raise ExecutionError(f"{failed} file(s) failed to move. Check output above.")


def _short_dest(dest: Path, source_root: Path | None) -> str:
    """Return a compact destination: Show/Season/file.ext or Movie (Year)/file.ext."""
    parts = dest.parts
    # Show last 3 parts (show/season/file or movie-folder/file) if deep enough
    if len(parts) >= 3:
        return str(Path(*parts[-3:]))
    if len(parts) >= 2:
        return str(Path(*parts[-2:]))
    return dest.name


_PLAN_ROW_LIMIT = 50  # keep the terminal usable on big libraries


def _print_plan(
    plan: Plan,
    source_root: Path | None = None,
    *,
    full_plan: bool = False,
) -> None:
    """Render the move/skip plan, truncating each section to keep output sane.

    Pass ``full_plan=True`` to dump every row. Without it, sections longer than
    ``_PLAN_ROW_LIMIT`` are clipped with a ``... and N more`` footer line.
    """
    if plan.moves:
        table = Table(title="Move plan", show_lines=False, expand=False)
        table.add_column("", width=2, no_wrap=True)
        table.add_column("Source file", style="cyan", no_wrap=False, max_width=40)
        table.add_column("Destination", style="green", no_wrap=False, max_width=50)
        table.add_column("TMDB match", style="white", max_width=30)
        table.add_column("Conf", style="dim", width=5, no_wrap=True)

        rows = plan.moves if full_plan else plan.moves[:_PLAN_ROW_LIMIT]
        for move in rows:
            icon = _TYPE_ICON.get(move.media_type, "?")
            conf_style = (
                "[bold green]high[/bold green]"
                if move.confidence == "high"
                else "[yellow]low[/yellow]"
            )
            table.add_row(
                icon,
                move.source.name,
                _short_dest(move.destination, source_root),
                move.matched_title,
                conf_style,
            )
        console.print(table)
        hidden = len(plan.moves) - len(rows)
        if hidden:
            console.print(
                f"[dim]  … and {hidden} more move(s) not shown — pass --full-plan to see them.[/dim]"
            )

    if plan.skipped:
        console.print(f"\n[yellow]Skipped ({len(plan.skipped)}):[/yellow]")
        rows_skipped = plan.skipped if full_plan else plan.skipped[:_PLAN_ROW_LIMIT]
        for skip in rows_skipped:
            console.print(f"  [yellow]⚠[/yellow] {skip.source.name} — {skip.skip_reason}")
        hidden_skipped = len(plan.skipped) - len(rows_skipped)
        if hidden_skipped:
            console.print(
                f"[dim]  … and {hidden_skipped} more skipped — pass --full-plan to see them.[/dim]"
            )
