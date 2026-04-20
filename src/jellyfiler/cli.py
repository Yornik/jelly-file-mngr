"""CLI entry point."""

import os
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from jellyfiler.anilist import looks_like_anime, search_anime
from jellyfiler.cache import _DEFAULT_DB, Cache
from jellyfiler.executor import ExecutionError, execute
from jellyfiler.guesser import guess
from jellyfiler.interactive import prompt_manual_title, prompt_tmdb_match
from jellyfiler.junk import is_junk, move_junk, report_junk
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


def _fmt_size(total_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if total_bytes < 1024:
            return f"{total_bytes:.0f} {unit}"
        total_bytes //= 1024
    return f"{total_bytes:.0f} TB"


def _print_summary(
    planned: int,
    skipped: int,
    junk_count: int,
    junk_bytes: int,
    tmdb_errors: int,
    dry_run: bool,
) -> None:
    lines = [
        f"  [green]✓[/green]  Planned moves   [bold]{planned:>5}[/bold]",
        f"  [yellow]⚠[/yellow]  Skipped         [bold]{skipped:>5}[/bold]",
        f"  [dim]🗑  Junk files     [bold]{junk_count:>5}[/bold]  ({_fmt_size(junk_bytes)})[/dim]",
    ]
    if tmdb_errors:
        lines.append(f"  [red]✗[/red]  TMDB errors     [bold]{tmdb_errors:>5}[/bold]")
    if dry_run:
        lines.append("\n  [bold cyan]DRY RUN[/bold cyan] — pass --apply to move files")
    console.print(Panel("\n".join(lines), title="[bold]Summary[/bold]", border_style="cyan"))


@app.command()
def organize(
    source: Annotated[Path, typer.Argument(help="Source directory containing media files")],
    dest: Annotated[
        Path | None,
        typer.Argument(help="Destination root. Omit when using --in-place."),
    ] = None,
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
    in_place: Annotated[
        bool,
        typer.Option(
            "--in-place",
            help="Reorganize within SOURCE itself instead of copying to a separate DEST.",
        ),
    ] = False,
    cleanup_empty_dirs: Annotated[
        bool,
        typer.Option(
            "--cleanup-empty-dirs",
            help="Remove empty source directories after moving files (only with --in-place --apply).",
        ),
    ] = False,
    cache_db: Annotated[
        Path,
        typer.Option("--cache-db", help="Path to the SQLite cache database."),
    ] = _DEFAULT_DB,
) -> None:
    """Scan SOURCE, match against TMDB, and organize into DEST.

    Pass --in-place to reorganize within SOURCE itself (no separate DEST needed).
    Dry-run by default — pass --apply to move files.
    Pass --interactive to be prompted when matches are uncertain.

    Set the TMDB_API_KEY environment variable before running.

    Data safety: nothing is ever deleted or overwritten.
    Pre-flight checks run before any file is touched. On any failure the
    operation aborts immediately with a clear message.
    """
    # Resolve destination
    if in_place:
        if dest is not None:
            err_console.print(
                "[bold red]Error:[/bold red] Cannot combine --in-place with a DEST argument."
            )
            raise typer.Exit(1)
        dest = source
        console.print(f"[bold yellow]IN-PLACE mode — reorganizing within {source}[/bold yellow]")
    elif dest is None:
        err_console.print("[bold red]Error:[/bold red] DEST is required unless --in-place is used.")
        raise typer.Exit(1)

    if cleanup_empty_dirs and not in_place:
        err_console.print(
            "[bold red]Error:[/bold red] --cleanup-empty-dirs only makes sense with --in-place."
        )
        raise typer.Exit(1)

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
    console.print(f"[dim]Cache: {cache_db}[/dim]\n")

    planned_moves: list[PlannedMove] = []
    junk_files: list[Path] = []
    tmdb_errors = 0
    _tmdb_consecutive_server_errors = 0
    _TMDB_CIRCUIT_BREAK = 3
    cache = Cache(cache_db)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Starting...", total=len(files))

        for file in files:
            label = file.name if len(file.name) <= 55 else file.name[:52] + "..."
            progress.update(task, description=f"[cyan]{label}[/cyan]")

            # Skip files already successfully moved in a previous run
            if cache.already_moved(file):
                console.print(f"[dim]SKIP (cached):[/dim] {file.name}")
                progress.advance(task)
                continue

            # Identify junk before any title parsing so junk never triggers a prompt
            if is_junk(file):
                console.print(f"[dim]JUNK:[/dim] {file.name}")
                junk_files.append(file)
                progress.advance(task)
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
                progress.advance(task)
                continue

            # Missing title — prompt if interactive, else skip
            if not guessed.title:
                if interactive:
                    progress.stop()
                    manual = prompt_manual_title(file.name, "")
                    progress.start()
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
                        progress.advance(task)
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
                    progress.advance(task)
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
                _tmdb_consecutive_server_errors = 0  # reset on success

            except httpx.HTTPStatusError as exc:
                is_server_error = exc.response.status_code >= 500
                err_console.print(
                    f"[red]TMDB error for '{file.name}': "
                    f"{exc.response.status_code} {exc.response.reason_phrase}[/red]"
                )
                tmdb_errors += 1
                if is_server_error:
                    _tmdb_consecutive_server_errors += 1
                    if _tmdb_consecutive_server_errors >= _TMDB_CIRCUIT_BREAK:
                        progress.stop()
                        err_console.print(
                            f"\n[bold red]TMDB is returning server errors — stopping after "
                            f"{_tmdb_consecutive_server_errors} consecutive failures. "
                            "Try again later.[/bold red]"
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
                                skip_reason=f"TMDB server error: {exc.response.status_code}",
                            )
                        )
                        break
                planned_moves.append(
                    PlannedMove(
                        source=file,
                        destination=dest,
                        media_type=guessed.media_type,
                        tmdb_id=None,
                        matched_title=guessed.title,
                        confidence="low",
                        skipped=True,
                        skip_reason=f"TMDB HTTP error: {exc.response.status_code}",
                    )
                )
                progress.advance(task)
                continue

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
                progress.advance(task)
                continue

            match = _resolve_match(
                file, guessed.title, guessed.year, matches, guessed.media_type, interactive
            )

            # AniList fallback: if TMDB missed and this looks like anime, try AniList
            if (
                match is None
                and guessed.media_type == MediaType.EPISODE
                and looks_like_anime(file.name)
            ):
                try:
                    al_cached = cache.get_tmdb(guessed.title, guessed.year, MediaType.EPISODE)
                    if al_cached is None:
                        al_matches = search_anime(guessed.title)
                        cache.set_tmdb(guessed.title, guessed.year, MediaType.EPISODE, al_matches)
                    else:
                        al_matches = al_cached
                    if al_matches:
                        console.print(
                            f"[dim]TMDB missed '{guessed.title}' — trying AniList...[/dim]"
                        )
                        if interactive:
                            progress.stop()
                        match = _resolve_match(
                            file,
                            guessed.title,
                            guessed.year,
                            al_matches,
                            guessed.media_type,
                            interactive,
                        )
                        if interactive:
                            progress.start()
                except Exception as exc:
                    console.print(f"[dim]AniList fallback failed for '{file.name}': {exc}[/dim]")

            if not match and not interactive and matches:
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
                progress.advance(task)
                continue

            if interactive and match is None and not matches:
                progress.stop()
            planned_moves.append(plan_move(guessed, match, dest, file))
            if interactive and match is None and not matches:
                progress.start()

            progress.advance(task)

    # Junk: report + move
    junk_bytes = sum(f.stat().st_size for f in junk_files if f.exists())
    if junk_files:
        report_junk(junk_files, source, dest, dry_run)
        if not dry_run:
            moved_junk, failed_junk = move_junk(junk_files, source, dest)
            if failed_junk:
                err_console.print(
                    f"[yellow]{failed_junk} junk file(s) could not be moved.[/yellow]"
                )

    plan = build_plan(planned_moves)

    try:
        execute(plan, dry_run=dry_run, cache=cache, source_root=source)
    except ExecutionError as exc:
        err_console.print(f"\n[bold red]{exc}[/bold red]")
        raise typer.Exit(1) from exc

    _print_summary(
        planned=len(plan.moves),
        skipped=len(plan.skipped),
        junk_count=len(junk_files),
        junk_bytes=junk_bytes,
        tmdb_errors=tmdb_errors,
        dry_run=dry_run,
    )

    if tmdb_errors:
        err_console.print(f"\n[yellow]{tmdb_errors} TMDB error(s) occurred — see above.[/yellow]")
        raise typer.Exit(1)

    # Clean up empty source directories (in-place + apply only)
    if in_place and apply and cleanup_empty_dirs and not dry_run:
        _remove_empty_dirs(source)


def _remove_empty_dirs(root: Path) -> None:
    """Recursively remove empty directories under root (but not root itself)."""
    removed = 0
    # Walk bottom-up so children are processed before parents
    for dirpath in sorted(root.rglob("*"), reverse=True):
        if dirpath == root:
            continue
        if dirpath.is_dir():
            try:
                dirpath.rmdir()  # only succeeds if directory is empty
                console.print(f"[dim]Removed empty dir: {dirpath}[/dim]")
                removed += 1
            except OSError:
                pass  # not empty, skip
    if removed:
        console.print(
            f"[dim]Cleaned up {removed} empty director{'y' if removed == 1 else 'ies'}.[/dim]"
        )
