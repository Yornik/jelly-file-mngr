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

from rich.console import Console

from jellyfiler.models import Plan, PlannedMove

console = Console()


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
            problems.append(
                f"Destination already exists (would overwrite): {move.destination}"
            )

        if move.destination in seen_destinations:
            problems.append(
                f"Duplicate destination in plan — two files would land at: {move.destination}"
            )
        seen_destinations.add(move.destination)

    return problems


def execute(plan: Plan, dry_run: bool = True) -> None:
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
    _print_plan(plan)

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
                console.print(
                    f"[red]SKIP (appeared since preflight): {move.destination}[/red]"
                )
                failed += 1
                continue

            shutil.move(str(move.source), str(move.destination))
            console.print(f"[green]✓[/green] {move.source.name}  →  {move.destination}")
            moved += 1

        except Exception as exc:
            console.print(f"[red]✗ FAILED:[/red] {move.source.name} — {exc}")
            failed += 1

    console.print(
        f"\n[bold]Done.[/bold] {moved} moved, {failed} failed, {len(plan.skipped)} skipped."
    )
    if failed:
        raise ExecutionError(f"{failed} file(s) failed to move. Check output above.")


def _print_plan(plan: Plan) -> None:
    from rich.table import Table

    table = Table(title="Move plan", show_lines=False)
    table.add_column("Source", style="cyan", no_wrap=False)
    table.add_column("Destination", style="green", no_wrap=False)
    table.add_column("Match", style="white")
    table.add_column("Conf", style="yellow")

    for move in plan.moves:
        table.add_row(
            str(move.source),
            str(move.destination),
            move.matched_title,
            move.confidence,
        )

    console.print(table)

    if plan.skipped:
        console.print(f"\n[yellow]Skipped ({len(plan.skipped)}):[/yellow]")
        for skip in plan.skipped:
            console.print(f"  [yellow]⚠[/yellow] {skip.source.name} — {skip.skip_reason}")
