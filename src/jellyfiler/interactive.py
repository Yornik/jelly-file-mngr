"""Interactive prompts for ambiguous matches."""

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

    Auto-selects when there is exactly one result.
    Returns the chosen match, or None if the user skips.
    """
    if len(matches) == 1:
        console.print(
            f"[dim]Auto-selected only match for '{guessed_title}': "
            f"{matches[0].title} ({matches[0].year})[/dim]"
        )
        return matches[0]

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


def prompt_episode_number(
    filename: str,
    episodes: list[tuple[int, str]],
) -> int | None:
    """Show a numbered list of episode titles and ask the user to pick one.

    Returns the episode number (not the list index), or None if the user skips.
    """
    console.print(
        f"\n[bold yellow]No episode number found for:[/bold yellow] [cyan]{filename}[/cyan]"
    )
    console.print("  Pick the episode from the season list below:\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=4)
    table.add_column("Ep", width=5)
    table.add_column("Title")

    display = episodes[:20]
    for i, (ep_num, ep_title) in enumerate(display, start=1):
        table.add_row(str(i), str(ep_num), ep_title)

    console.print(table)
    console.print("[dim]Enter a list number to select, 0 to skip, or press Enter to skip.[/dim]")

    raw = typer.prompt("Choice", default="0")
    try:
        choice = int(raw.strip())
    except ValueError:
        console.print("[yellow]Invalid input — skipping.[/yellow]")
        return None

    if choice == 0 or not raw.strip():
        console.print("[yellow]Skipped.[/yellow]")
        return None

    if 1 <= choice <= len(display):
        ep_num, ep_title = display[choice - 1]
        console.print(f"[green]Selected:[/green] E{ep_num:02d} — {ep_title}")
        return ep_num

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


def prompt_duplicate_choice(group):  # type: ignore[no-untyped-def]
    """Ask the user how to resolve a duplicate-destination group.

    `group` is a list of PlannedMove sorted highest-quality-first. Returns a
    DuplicateChoice. Imported lazily so dedupe.py and interactive.py don't
    have a circular dependency.
    """
    from jellyfiler.dedupe import DuplicateChoice, describe

    dest = group[0].destination

    console.print("\n[bold yellow]Duplicate detected[/bold yellow] — both would land at:")
    console.print(f"  [cyan]{dest}[/cyan]\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=3)
    table.add_column("Source file (highest quality first)")
    for i, m in enumerate(group, start=1):
        table.add_row(str(i), describe(m))
    console.print(table)

    console.print(
        "\n  [bold]1..N[/bold]  keep that file, skip the others (others stay in source)\n"
        "  [bold]s[/bold]     skip all (move none of them)\n"
        "  [bold]a[/bold]     always keep highest quality for the rest of this run "
        "(losers stay in source)\n"
        "  [bold]q[/bold]     always keep highest quality + quarantine losers to "
        ".aside/duplicates/ (recoverable)\n"
        "  [bold red]d[/bold red]     [bold red]DELETE the lower-quality file(s) AND their parent "
        "directories[/bold red] (this group only — not sticky)\n"
    )

    raw = typer.prompt("Choice", default="1").strip().lower()

    if raw == "s":
        return DuplicateChoice(DuplicateChoice.SKIP_ALL)
    if raw == "a":
        return DuplicateChoice(DuplicateChoice.ALWAYS_HIGHER)
    if raw == "q":
        return DuplicateChoice(DuplicateChoice.ALWAYS_QUARANTINE)
    if raw == "d":
        # DELETE_LOSERS: keep the highest-quality (index 0), nuke the rest + their dirs.
        return DuplicateChoice(DuplicateChoice.DELETE_LOSERS, index=0)
    try:
        idx = int(raw)
    except ValueError:
        console.print("[yellow]Invalid input — defaulting to highest quality (1).[/yellow]")
        idx = 1
    if idx < 1 or idx > len(group):
        console.print("[yellow]Out of range — defaulting to highest quality (1).[/yellow]")
        idx = 1
    return DuplicateChoice(DuplicateChoice.KEEP_INDEX, index=idx - 1)
