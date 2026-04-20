"""Execute a plan by safely moving files.

Data safety rules:
- Never delete anything.
- Never overwrite an existing destination.
- Create destination directories only when moving, never pre-emptively.
- Abort the entire operation if any pre-flight check fails.
- All moves are logged before execution.
"""

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from jellyfiler.models import MediaType, Plan, PlannedMove

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
) -> None:
    """Execute the plan.

    In dry-run mode (default) nothing is touched — the plan is printed only.
    Pass dry_run=False to actually move files.

    Aborts before touching anything if any pre-flight check fails.
    """
    if not plan.moves:
        console.print("[yellow]Nothing to move.[/yellow]")
        return

    if dry_run:
        console.print("\n[bold cyan]DRY RUN — no files will be moved[/bold cyan]\n")
    else:
        console.print("\n[bold red]LIVE RUN — files will be moved[/bold red]\n")

    # Always show the full plan
    _print_plan(plan, source_root)

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

            shutil.move(str(move.source), str(move.destination))
            console.print(f"[green]✓[/green] {move.source.name}  →  {move.destination}")
            if cache is not None:
                cache.record_move(move.source, move.destination)
            moved += 1

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


def _print_plan(plan: Plan, source_root: Path | None = None) -> None:
    table = Table(title="Move plan", show_lines=False, expand=False)
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Source file", style="cyan", no_wrap=False, max_width=40)
    table.add_column("Destination", style="green", no_wrap=False, max_width=50)
    table.add_column("TMDB match", style="white", max_width=30)
    table.add_column("Conf", style="dim", width=5, no_wrap=True)

    for move in plan.moves:
        icon = _TYPE_ICON.get(move.media_type, "?")
        conf_style = (
            "[bold green]high[/bold green]" if move.confidence == "high" else "[yellow]low[/yellow]"
        )
        table.add_row(
            icon,
            move.source.name,
            _short_dest(move.destination, source_root),
            move.matched_title,
            conf_style,
        )

    console.print(table)

    if plan.skipped:
        console.print(f"\n[yellow]Skipped ({len(plan.skipped)}):[/yellow]")
        for skip in plan.skipped:
            console.print(f"  [yellow]⚠[/yellow] {skip.source.name} — {skip.skip_reason}")
