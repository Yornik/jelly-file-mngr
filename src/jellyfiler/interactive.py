"""Interactive prompts for ambiguous matches."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from jellyfiler.models import MediaType, TmdbMatch

console = Console()


def prompt_tmdb_match(
    filename: str,
    guessed_title: str,
    matches: list[TmdbMatch],
    media_type: MediaType,
) -> TmdbMatch | None:
    """Show the user a list of TMDB results and ask them to pick one.

    Returns the chosen match, or None if the user skips.
    """
    console.print(f"\n[bold yellow]Ambiguous match for:[/bold yellow] [cyan]{filename}[/cyan]")
    console.print(f"  guessit parsed title: [bold]{guessed_title}[/bold]")
    console.print(f"  media type: [bold]{media_type.value}[/bold]\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=4)
    table.add_column("Title")
    table.add_column("Year", width=6)
    table.add_column("TMDB ID", width=10)

    display_matches = matches[:10]  # never show more than 10 options
    for i, m in enumerate(display_matches, start=1):
        table.add_row(str(i), m.title, str(m.year or ""), str(m.tmdb_id))

    console.print(table)
    console.print("[dim]Enter a number to select, 0 to skip, or press Enter to skip.[/dim]")

    raw = typer.prompt("Choice", default="0")
    try:
        choice = int(raw.strip())
    except ValueError:
        console.print("[yellow]Invalid input — skipping.[/yellow]")
        return None

    if choice == 0 or not raw.strip():
        console.print("[yellow]Skipped.[/yellow]")
        return None

    if 1 <= choice <= len(display_matches):
        selected = display_matches[choice - 1]
        console.print(f"[green]Selected:[/green] {selected.title} ({selected.year})")
        return selected

    console.print("[yellow]Out of range — skipping.[/yellow]")
    return None


def prompt_manual_title(filename: str, guessed_title: str) -> str | None:
    """Ask the user to provide or confirm a title when guessit fails."""
    console.print(
        f"\n[bold yellow]Could not parse a title from:[/bold yellow] [cyan]{filename}[/cyan]"
    )
    raw = typer.prompt(
        "Enter title to search (or press Enter to skip)",
        default="",
    )
    return raw.strip() or None
