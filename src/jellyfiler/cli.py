"""CLI entry point."""

import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from jellyfiler.cache import _DEFAULT_DB, Cache
from jellyfiler.executor import ExecutionError, execute
from jellyfiler.guesser import guess
from jellyfiler.interactive import prompt_manual_title, prompt_tmdb_match
from jellyfiler.models import MediaType, PlannedMove
from jellyfiler.planner import build_plan, plan_move
from jellyfiler.scanner import find_media_files
from jellyfiler.tmdb import TmdbClient, TmdbMatch, best_match

app = typer.Typer(
    name="jellyfiler",
    help="Organize media rips into a Jellyfin-compatible directory structure.",
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def _get_tmdb_client() -> TmdbClient:
    api_key = os.environ.get("TMDB_API_KEY", "")
    if not api_key:
        err_console.print(
            "[bold red]Error:[/bold red] TMDB_API_KEY environment variable is not set.\n"
            "Get a free key at https://www.themoviedb.org/settings/api"
        )
        raise typer.Exit(1)
    return TmdbClient(api_key=api_key)


def _resolve_match(
    file: Path,
    guessed_title: str,
    guessed_year: int | None,
    matches: list[TmdbMatch],
    media_type: MediaType,
    interactive: bool,
) -> TmdbMatch | None:
    """Return the best match, prompting the user if interactive and result is ambiguous."""
    match = best_match(matches, guessed_title, guessed_year)

    if match:
        return match

    # No confident match found
    if interactive and matches:
        # We have TMDB results but none matched confidently — let the user pick
        return prompt_tmdb_match(file.name, guessed_title, matches, media_type)

    return None


@app.command()
def organize(
    source: Annotated[Path, typer.Argument(help="Source directory containing media files")],
    dest: Annotated[Path, typer.Argument(help="Destination root for organized output")],
    media_type: Annotated[
        MediaType,
        typer.Option("--type", "-t", help="Force media type (movie or episode)"),
    ] = MediaType.UNKNOWN,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually move files. Without this, dry-run only."),
    ] = False,
    interactive: Annotated[
        bool,
        typer.Option(
            "--interactive/--no-interactive",
            help="Prompt for input when a match is ambiguous or missing (default: on).",
        ),
    ] = True,
    cache_db: Annotated[
        Path,
        typer.Option("--cache-db", help="Path to the SQLite cache database."),
    ] = _DEFAULT_DB,
) -> None:
    """Scan SOURCE, match against TMDB, and organize into DEST.

    Dry-run by default — pass --apply to move files.
    Pass --interactive to be prompted when matches are uncertain.

    Set the TMDB_API_KEY environment variable before running.

    Data safety: nothing is ever deleted or overwritten.
    Pre-flight checks run before any file is touched. On any failure the
    operation aborts immediately with a clear message.
    """
    dry_run = not apply

    if dry_run:
        console.print("[bold cyan]DRY-RUN mode — no files will be moved (use --apply)[/bold cyan]")

    if interactive:
        console.print(
            "[bold magenta]Interactive mode — you will be prompted on ambiguous matches[/bold magenta]"
        )

    tmdb = _get_tmdb_client()

    console.print(f"\nScanning [cyan]{source}[/cyan]...")
    try:
        files = find_media_files(source)
    except (FileNotFoundError, NotADirectoryError) as exc:
        err_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1) from exc

    if not files:
        console.print("[yellow]No media files found.[/yellow]")
        raise typer.Exit(0)

    console.print(f"Found [bold]{len(files)}[/bold] media files. Querying TMDB...\n")

    planned_moves: list[PlannedMove] = []
    tmdb_errors = 0
    cache = Cache(cache_db)
    console.print(f"[dim]Cache: {cache_db}[/dim]")

    for file in files:
        # Skip files already successfully moved in a previous run
        if cache.already_moved(file):
            console.print(f"[dim]SKIP (already moved in previous run):[/dim] {file.name}")
            continue

        guessed = guess(file)

        # Allow user to override detected type
        if media_type != MediaType.UNKNOWN:
            guessed.media_type = media_type

        # Unknown type — skip or ask
        if guessed.media_type == MediaType.UNKNOWN:
            console.print(f"[yellow]SKIP (unknown type):[/yellow] {file.name}")
            planned_moves.append(
                PlannedMove(
                    source=file,
                    destination=dest,
                    media_type=MediaType.UNKNOWN,
                    tmdb_id=None,
                    matched_title=guessed.title or file.name,
                    confidence="low",
                    skipped=True,
                    skip_reason="Could not determine media type — pass --type to force",
                )
            )
            continue

        # Missing title — prompt if interactive, else skip
        if not guessed.title:
            if interactive:
                manual = prompt_manual_title(file.name, "")
                if manual:
                    guessed.title = manual
                else:
                    planned_moves.append(
                        PlannedMove(
                            source=file,
                            destination=dest,
                            media_type=guessed.media_type,
                            tmdb_id=None,
                            matched_title=file.name,
                            confidence="low",
                            skipped=True,
                            skip_reason="User skipped — no title provided",
                        )
                    )
                    continue
            else:
                console.print(f"[yellow]SKIP (no title parsed):[/yellow] {file.name}")
                planned_moves.append(
                    PlannedMove(
                        source=file,
                        destination=dest,
                        media_type=guessed.media_type,
                        tmdb_id=None,
                        matched_title=file.name,
                        confidence="low",
                        skipped=True,
                        skip_reason="guessit could not extract a title — run with --interactive",
                    )
                )
                continue

        # TMDB lookup — SQLite cache first, then API
        try:
            cached = cache.get_tmdb(guessed.title, guessed.year, guessed.media_type)
            if cached is not None:
                matches = cached
            elif guessed.media_type == MediaType.MOVIE:
                matches = tmdb.search_movie(guessed.title, guessed.year)
                cache.set_tmdb(guessed.title, guessed.year, guessed.media_type, matches)
            else:
                matches = tmdb.search_tv(guessed.title, guessed.year)
                cache.set_tmdb(guessed.title, guessed.year, guessed.media_type, matches)

        except Exception as exc:
            err_console.print(f"[red]TMDB error for '{file.name}': {exc}[/red]")
            tmdb_errors += 1
            planned_moves.append(
                PlannedMove(
                    source=file,
                    destination=dest,
                    media_type=guessed.media_type,
                    tmdb_id=None,
                    matched_title=guessed.title,
                    confidence="low",
                    skipped=True,
                    skip_reason=f"TMDB query failed: {exc}",
                )
            )
            continue

        match = _resolve_match(
            file, guessed.title, guessed.year, matches, guessed.media_type, interactive
        )

        if not match and not interactive and matches:
            # Non-interactive, no confident match but results exist — prompt anyway
            # since ambiguity is dangerous for file operations
            console.print(
                f"[yellow]SKIP (ambiguous):[/yellow] '{guessed.title}' — "
                f"{len(matches)} TMDB results, none matched confidently. "
                "Run with --interactive to pick manually."
            )
            planned_moves.append(
                PlannedMove(
                    source=file,
                    destination=dest,
                    media_type=guessed.media_type,
                    tmdb_id=None,
                    matched_title=guessed.title,
                    confidence="low",
                    skipped=True,
                    skip_reason=f"Ambiguous: {len(matches)} results, no confident match. Use --interactive.",
                )
            )
            continue

        planned_moves.append(plan_move(guessed, match, dest, file))

    plan = build_plan(planned_moves)

    try:
        execute(plan, dry_run=dry_run, cache=cache)
    except ExecutionError as exc:
        err_console.print(f"\n[bold red]{exc}[/bold red]")
        raise typer.Exit(1) from exc

    if tmdb_errors:
        err_console.print(f"\n[yellow]{tmdb_errors} TMDB error(s) occurred — see above.[/yellow]")
        raise typer.Exit(1)
