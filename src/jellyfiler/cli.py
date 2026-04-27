"""CLI entry point."""

import os
import re
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from jellyfiler.ai_query import AiQueryError, preflight_check, suggest_search
from jellyfiler.anilist import looks_like_anime, search_anime
from jellyfiler.cache import _DEFAULT_DB, Cache
from jellyfiler.dedupe import (
    find_duplicate_groups,
    quarantine_path,
    resolve_duplicates,
)
from jellyfiler.executor import ExecutionError, execute
from jellyfiler.guesser import guess
from jellyfiler.interactive import (
    prompt_duplicate_choice,
    prompt_episode_number,
    prompt_manual_title,
    prompt_tmdb_match,
)
from jellyfiler.junk import is_junk, move_junk, report_junk
from jellyfiler.models import MediaType, PlannedMove
from jellyfiler.planner import build_plan, plan_move
from jellyfiler.scanner import find_media_files
from jellyfiler.tmdb import TmdbClient, TmdbMatch, best_match

__version__ = "0.1.0"


def _version_callback(value: bool) -> None:
    if value:
        console_plain = Console()
        console_plain.print(f"jellyfiler {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="jellyfiler",
    help="Organize media rips into a Jellyfin-compatible directory structure.",
    add_completion=False,
)
cache_app = typer.Typer(name="cache", help="Inspect and manage the jellyfiler SQLite cache.")
app.add_typer(cache_app)

console = Console()
err_console = Console(stderr=True)


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    pass


def _get_tmdb_client() -> TmdbClient:
    api_key = os.environ.get("TMDB_API_KEY", "")
    if not api_key:
        err_console.print(
            "[bold red]Error:[/bold red] TMDB_API_KEY environment variable is not set.\n"
            "Get a free key at https://www.themoviedb.org/settings/api"
        )
        raise typer.Exit(1)
    return TmdbClient(api_key=api_key)


_ROMAN_SUFFIX = re.compile(r"\s+[IVXLCDM]+$", re.IGNORECASE)
# CamelCase / run-together words: "wonderwoman" → "wonder woman"
_CAMEL_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _strip_roman_suffix(title: str) -> str:
    """Strip a trailing Roman numeral from a title ('Superman I' → 'Superman')."""
    return _ROMAN_SUFFIX.sub("", title).strip()


def _title_variants(title: str) -> list[str]:
    """Return alternative search strings to try when the canonical title misses."""
    import wordninja

    variants: list[str] = []
    # Strip trailing Roman numeral: "Superman I" → "Superman"
    stripped = _strip_roman_suffix(title)
    if stripped != title:
        variants.append(stripped)
    # Replace & with 'and': "Superman & Batman" → "Superman and Batman"
    if "&" in title:
        variants.append(title.replace("&", "and").replace("  ", " ").strip())
    # Split CamelCase: "WonderWoman" → "Wonder Woman"
    spaced = _CAMEL_SPLIT.sub(" ", title)
    if spaced != title:
        variants.append(spaced)
    # Word-segment run-together lowercase: "wonderwoman" → "wonder woman"
    if " " not in title and title == title.lower():
        segmented = " ".join(wordninja.split(title))
        if segmented != title:
            variants.append(segmented)
    return variants


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
    rich_names: Annotated[
        bool,
        typer.Option(
            "--rich-names",
            help="Include episode title, series title, and quality in the destination filename: S01E01-Episode Title-Show Name-720p.ext",
        ),
    ] = False,
    quarantine_duplicates: Annotated[
        bool,
        typer.Option(
            "--quarantine-duplicates",
            help=(
                "Auto-resolve duplicate destinations: keep the highest-quality file "
                "and move losers to dest/.junk/duplicates/ (recoverable). "
                "Cron-friendly: no extra confirmation flag needed since it's reversible."
            ),
        ),
    ] = False,
    remove_duplicates: Annotated[
        bool,
        typer.Option(
            "--remove-duplicates",
            help=(
                "Auto-resolve duplicate destinations: keep the highest-quality file "
                "and PERMANENTLY DELETE the rest. This is the only flag in the tool "
                "that deletes user files. Requires --i-mean-it to actually run."
            ),
        ),
    ] = False,
    i_mean_it: Annotated[
        bool,
        typer.Option(
            "--i-mean-it",
            help=(
                "Required confirmation alongside --remove-duplicates. "
                "Without this flag, --remove-duplicates aborts with a warning. "
                "This double-flag protects against accidental deletion in cron jobs."
            ),
        ),
    ] = False,
    cache_db: Annotated[
        Path,
        typer.Option("--cache-db", help="Path to the SQLite cache database."),
    ] = _DEFAULT_DB,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Process at most N files (useful for test runs)."),
    ] = 0,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-process files already recorded in the move log."),
    ] = False,
    dry_run_flag: Annotated[
        bool,
        typer.Option("--dry-run", help="Explicit dry-run flag (same as omitting --apply)."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress per-file output; show summary only."),
    ] = False,
    use_ai: Annotated[
        bool,
        typer.Option(
            "--use-ai",
            help="Enable Claude Haiku AI fallback for hard-to-parse titles (requires ANTHROPIC_API_KEY).",
        ),
    ] = False,
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

    # Safety gate for the auto-dedupe feature: --remove-duplicates without
    # --i-mean-it always aborts with a big red warning, so a typo or a
    # misconfigured cron job can't quietly start deleting files.
    if remove_duplicates and not i_mean_it:
        err_console.print(
            "\n"
            "[bold white on red]"
            " ╔════════════════════════════════════════════════════════════════════╗ \n"
            " ║              ⚠  PERMANENT FILE DELETION REQUESTED  ⚠               ║ \n"
            " ║                                                                    ║ \n"
            " ║   --remove-duplicates will PERMANENTLY DELETE the lower-quality    ║ \n"
            " ║   copy of every duplicate-destination pair found on this run.      ║ \n"
            " ║   Files are unlinked from disk. There is no undo.                  ║ \n"
            " ║                                                                    ║ \n"
            " ║   To actually run, you MUST also pass --i-mean-it.                 ║ \n"
            " ║   This double-flag protects against accidents and cron typos.      ║ \n"
            " ╚════════════════════════════════════════════════════════════════════╝ "
            "[/bold white on red]"
        )
        err_console.print(
            "\n[bold red]Aborting. Add --i-mean-it to confirm permanent deletion.[/bold red]\n"
        )
        raise typer.Exit(1)
    if i_mean_it and not remove_duplicates:
        err_console.print(
            "[bold red]Error:[/bold red] --i-mean-it has no effect without --remove-duplicates."
        )
        raise typer.Exit(1)
    if quarantine_duplicates and remove_duplicates:
        err_console.print(
            "[bold red]Error:[/bold red] Cannot combine --quarantine-duplicates with --remove-duplicates. "
            "Quarantine moves losers to .junk/duplicates/ (recoverable); remove deletes them."
        )
        raise typer.Exit(1)

    dry_run = not apply or dry_run_flag

    # --use-ai preflight: key must exist and Haiku must respond "true"
    if use_ai:
        ai_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not ai_key:
            err_console.print(
                "[bold red]Error:[/bold red] --use-ai requires ANTHROPIC_API_KEY to be set."
            )
            raise typer.Exit(1)
        if not quiet:
            console.print("[dim]Checking Anthropic API key...[/dim]")
        if not preflight_check(ai_key):
            err_console.print(
                "[bold red]Error:[/bold red] Anthropic API key check failed — "
                "verify ANTHROPIC_API_KEY is valid."
            )
            raise typer.Exit(1)
        if not quiet:
            console.print("[green]✓[/green] Anthropic API key OK")

    if not quiet:
        if dry_run:
            console.print(
                "[bold cyan]DRY-RUN mode — no files will be moved (use --apply)[/bold cyan]"
            )
        if interactive:
            console.print(
                "[bold magenta]Interactive mode — you will be prompted on ambiguous matches[/bold magenta]"
            )

    tmdb = _get_tmdb_client()

    if not quiet:
        console.print(f"\nScanning [cyan]{source}[/cyan]...")
    try:
        files = find_media_files(source)
    except (FileNotFoundError, NotADirectoryError) as exc:
        err_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1) from exc

    if not files:
        console.print("[yellow]No media files found.[/yellow]")
        raise typer.Exit(0)

    if limit:
        files = files[:limit]

    if not quiet:
        console.print(f"Found [bold]{len(files)}[/bold] media files. Querying TMDB...\n")
        console.print(f"[dim]Cache: {cache_db}[/dim]\n")

    planned_moves: list[PlannedMove] = []
    junk_files: list[Path] = []
    tmdb_errors = 0
    ai_disabled = False  # set to True if user opts out mid-run after an AI error
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

            # Skip files already successfully moved in a previous run (unless --force)
            if not force and cache.already_moved(file):
                if not quiet:
                    console.print(f"[dim]SKIP (cached):[/dim] {file.name}")
                progress.advance(task)
                continue

            # Identify junk before any title parsing so junk never triggers a prompt
            if is_junk(file):
                if not quiet:
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
                if not quiet:
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
                    if not quiet:
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

            # For TV shows the cache key always uses year=None (we never filter by year).
            _cache_year = guessed.year if guessed.media_type == MediaType.MOVIE else None

            # Check for a previously pinned interactive choice — skip TMDB entirely
            pinned = cache.get_pinned(guessed.title, _cache_year, guessed.media_type)
            if pinned:
                if not quiet:
                    console.print(
                        f"[dim]PINNED:[/dim] {guessed.title} → {pinned.title} ({pinned.year})"
                    )
                planned_moves.append(plan_move(guessed, pinned, dest, file, rich_names=rich_names))
                progress.advance(task)
                continue

            # TMDB lookup — SQLite cache first, then API
            try:
                cached = cache.get_tmdb(guessed.title, _cache_year, guessed.media_type)
                if cached is not None:
                    matches = cached
                elif guessed.media_type == MediaType.MOVIE:
                    matches = tmdb.search_movie(guessed.title, guessed.year)
                    cache.set_tmdb(guessed.title, guessed.year, guessed.media_type, matches)
                else:
                    # Never pass year for TV — TMDB's first_air_date_year is the
                    # show's premiere year, which never matches a season folder year.
                    matches = tmdb.search_tv(guessed.title, None)
                    cache.set_tmdb(guessed.title, None, guessed.media_type, matches)
            except httpx.HTTPStatusError as exc:
                progress.stop()
                err_console.print(
                    f"\n[bold red]TMDB error: {exc.response.status_code} "
                    f"{exc.response.reason_phrase} — stopping.[/bold red]"
                )
                tmdb_errors += 1
                break

            except Exception as exc:
                progress.stop()
                err_console.print(f"\n[bold red]TMDB error: {exc} — stopping.[/bold red]")
                tmdb_errors += 1
                break

            # Retry with title variants if no confident match on first search
            search_title = guessed.title
            if not best_match(matches, guessed.title, guessed.year):
                for variant in _title_variants(guessed.title):
                    try:
                        retry = (
                            tmdb.search_movie(variant, guessed.year)
                            if guessed.media_type == MediaType.MOVIE
                            else tmdb.search_tv(variant, None)
                        )
                        if retry and best_match(retry, variant, guessed.year):
                            matches = retry
                            search_title = variant
                            cache.set_tmdb(variant, _cache_year, guessed.media_type, retry)
                            break
                    except Exception:
                        pass

            # AniList fallback runs BEFORE AI: free + authoritative for anime,
            # so we don't burn AI tokens on titles that AniList would catch.
            if (
                not best_match(matches, search_title, guessed.year)
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
                        if not quiet:
                            console.print(
                                f"[dim]TMDB missed '{guessed.title}' — trying AniList...[/dim]"
                            )
                        matches = al_matches
                        search_title = guessed.title
                except Exception as exc:
                    console.print(f"[dim]AniList fallback failed for '{file.name}': {exc}[/dim]")

            # AI fallback: only if variants AND AniList both missed
            if use_ai and not ai_disabled and not best_match(matches, search_title, guessed.year):
                ai_key = os.environ.get("ANTHROPIC_API_KEY", "")
                if ai_key:
                    try:
                        suggestion = suggest_search(
                            file.parent.name,
                            file.name,
                            ai_key,
                            is_tv=guessed.media_type == MediaType.EPISODE,
                        )
                    except AiQueryError as exc:
                        progress.stop()
                        err_console.print(f"\n[bold red]Anthropic API error: {exc}[/bold red]")
                        if interactive:
                            disable = typer.confirm("Disable AI and continue without it?")
                            if disable:
                                ai_disabled = True
                                suggestion = None
                                progress.start()
                            else:
                                err_console.print("[bold red]Stopping.[/bold red]")
                                break
                        else:
                            err_console.print("[bold red]Stopping.[/bold red]")
                            break
                    else:
                        if suggestion:
                            ai_title = str(suggestion.get("title", ""))
                            ai_year_raw = suggestion.get("year")
                            ai_year = (
                                int(ai_year_raw) if isinstance(ai_year_raw, (int, float)) else None
                            )
                            if ai_title and ai_title != search_title:
                                console.print(
                                    f"[dim]AI query suggestion for '{guessed.title}': "
                                    f"'{ai_title}' ({ai_year})[/dim]"
                                )
                                try:
                                    ai_retry = (
                                        tmdb.search_movie(ai_title, ai_year)
                                        if guessed.media_type == MediaType.MOVIE
                                        else tmdb.search_tv(ai_title, None)
                                    )
                                    if ai_retry:
                                        matches = ai_retry
                                        search_title = ai_title
                                        cache.set_tmdb(
                                            ai_title, _cache_year, guessed.media_type, ai_retry
                                        )
                                except Exception:
                                    pass

            if interactive:
                progress.stop()
            match = _resolve_match(
                file, search_title, guessed.year, matches, guessed.media_type, interactive
            )
            if interactive:
                progress.start()

            if not match and not interactive and matches:
                if not quiet:
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

            # Bare episode file (no S/E marker) — ask user to identify the episode
            if (
                match is not None
                and guessed.media_type == MediaType.EPISODE
                and guessed.episode is None
                and interactive
            ):
                progress.stop()
                try:
                    season_num = guessed.season or 1
                    episodes = tmdb.get_season_episodes(match.tmdb_id, season_num)
                    if episodes:
                        picked = prompt_episode_number(file.name, episodes)
                        if picked is not None:
                            guessed.episode = picked
                except Exception as exc:
                    console.print(f"[dim]Could not fetch episode list: {exc}[/dim]")
                progress.start()

            if interactive and match is None and not matches:
                progress.stop()
            planned_moves.append(plan_move(guessed, match, dest, file, rich_names=rich_names))
            if interactive and match is None and not matches:
                progress.start()

            # Pin confirmed match so future runs skip the prompt
            if match:
                cache.set_pinned(search_title, _cache_year, guessed.media_type, match)

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

    # Resolve duplicate destinations before preflight (which would otherwise abort).
    duplicate_groups = find_duplicate_groups(plan.moves)
    losers_to_delete: list[PlannedMove] = []
    losers_to_quarantine: list[PlannedMove] = []
    dirs_to_remove: set[Path] = set()
    if duplicate_groups:
        if not quiet:
            console.print(
                f"\n[bold yellow]Duplicate destinations detected:[/bold yellow] "
                f"{len(duplicate_groups)} group(s) — resolving..."
            )
        # `--quarantine-duplicates` is a non-interactive auto-quarantine: always
        # keep the highest quality, move losers to .junk/duplicates/. We achieve
        # it by injecting a synthetic "always quarantine" prompt that fires once.
        from jellyfiler.dedupe import DuplicateChoice as _DC
        from jellyfiler.dedupe import PromptFn as _PromptFn

        prompt_fn: _PromptFn | None
        if quarantine_duplicates:

            def _quarantine_prompt(_group: list[PlannedMove]) -> _DC:
                return _DC(_DC.ALWAYS_QUARANTINE)

            prompt_fn = _quarantine_prompt
            run_interactive = True  # so resolve_duplicates calls our synthetic prompt
        elif interactive:
            prompt_fn = prompt_duplicate_choice
            run_interactive = True
        else:
            prompt_fn = None
            run_interactive = False
        plan, losers_to_delete, losers_to_quarantine, dirs_to_remove = resolve_duplicates(
            plan,
            interactive=run_interactive,
            auto_remove=remove_duplicates,
            prompt=prompt_fn,
        )
        if losers_to_delete and not quiet:
            console.print(
                f"[bold red]{len(losers_to_delete)} duplicate loser(s) will be PERMANENTLY DELETED[/bold red]"
            )
        if dirs_to_remove and not quiet:
            console.print(
                f"[bold red]{len(dirs_to_remove)} parent director{'y' if len(dirs_to_remove) == 1 else 'ies'} "
                f"will also be removed (per user choice)[/bold red]"
            )
        if losers_to_quarantine and not quiet:
            console.print(
                f"[yellow]{len(losers_to_quarantine)} duplicate loser(s) will be quarantined to "
                f"{dest / '.junk' / 'duplicates'}[/yellow]"
            )

    try:
        execute(plan, dry_run=dry_run, cache=cache, source_root=source)
    except ExecutionError as exc:
        err_console.print(f"\n[bold red]{exc}[/bold red]")
        raise typer.Exit(1) from exc

    # Handle duplicate losers AFTER execute() succeeds — never touch losers
    # if the main run failed.
    if not dry_run:
        if losers_to_quarantine:
            import shutil as _shutil

            for loser in losers_to_quarantine:
                qpath = quarantine_path(loser, source, dest)
                try:
                    qpath.parent.mkdir(parents=True, exist_ok=True)
                    if qpath.exists():
                        console.print(f"[dim]  quarantine target exists, skipping: {qpath}[/dim]")
                        continue
                    _shutil.move(str(loser.source), str(qpath))
                    console.print(f"[dim]  duplicate → {qpath}[/dim]")
                except Exception as exc:
                    err_console.print(
                        f"[yellow]Could not quarantine {loser.source}: {exc}[/yellow]"
                    )

        if losers_to_delete:
            for loser in losers_to_delete:
                try:
                    if loser.source.exists():
                        loser.source.unlink()
                        console.print(f"[red]  deleted duplicate: {loser.source}[/red]")
                except Exception as exc:
                    err_console.print(f"[yellow]Could not delete {loser.source}: {exc}[/yellow]")

        if dirs_to_remove:
            import shutil as _shutil

            for d in dirs_to_remove:
                try:
                    if d.exists():
                        _shutil.rmtree(d)
                        console.print(f"[red]  removed directory: {d}[/red]")
                except Exception as exc:
                    err_console.print(f"[yellow]Could not remove {d}: {exc}[/yellow]")

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


@app.command()
def scan(
    source: Annotated[Path, typer.Argument(help="Directory to scan")],
) -> None:
    """Parse filenames with guessit and print what was detected — no TMDB calls.

    Useful for debugging why a filename is being misidentified.
    """
    from rich.table import Table

    try:
        files = find_media_files(source)
    except (FileNotFoundError, NotADirectoryError) as exc:
        err_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1) from exc

    if not files:
        console.print("[yellow]No media files found.[/yellow]")
        raise typer.Exit(0)

    table = Table(title=f"Parsed metadata — {source}", show_lines=False)
    table.add_column("Filename", style="cyan", max_width=45)
    table.add_column("Type", width=8)
    table.add_column("Title", style="white", max_width=30)
    table.add_column("Year", width=6)
    table.add_column("S", width=4)
    table.add_column("E", width=4)

    for f in files:
        g = guess(f)
        table.add_row(
            f.name,
            g.media_type.value,
            g.title or "[dim]—[/dim]",
            str(g.year) if g.year else "[dim]—[/dim]",
            str(g.season) if g.season is not None else "[dim]—[/dim]",
            str(g.episode) if g.episode is not None else "[dim]—[/dim]",
        )

    console.print(table)


# ── Cache subcommands ────────────────────────────────────────────────────────


@cache_app.command("stats")
def cache_stats(
    cache_db: Annotated[Path, typer.Option("--cache-db")] = _DEFAULT_DB,
) -> None:
    """Show row counts for each cache table."""
    cache = Cache(cache_db)
    s = cache.stats()
    cache.close()
    console.print(f"  TMDB search cache : [bold]{s['tmdb_cache']}[/bold] entries")
    console.print(f"  Pinned choices    : [bold]{s['pinned']}[/bold] entries")
    console.print(f"  Move log          : [bold]{s['move_log']}[/bold] files")
    console.print(f"  [dim]DB: {cache_db}[/dim]")


@cache_app.command("unpin")
def cache_unpin(
    title: Annotated[str, typer.Argument(help="Show/movie title to unpin")],
    media_type: Annotated[
        MediaType, typer.Option("--type", "-t", help="Media type (movie or episode)")
    ] = MediaType.EPISODE,
    year: Annotated[int, typer.Option("--year", "-y", help="Year (movies only)")] = 0,
    cache_db: Annotated[Path, typer.Option("--cache-db")] = _DEFAULT_DB,
) -> None:
    """Remove a pinned TMDB match so the title is re-prompted on next run."""
    cache = Cache(cache_db)
    removed = cache.unpin(title, year if year else None, media_type)
    cache.close()
    if removed:
        console.print(f"[green]Unpinned:[/green] '{title}'")
    else:
        console.print(f"[yellow]Not found:[/yellow] no pinned entry for '{title}'")


@cache_app.command("clear")
def cache_clear(
    pinned: Annotated[bool, typer.Option("--pinned", help="Clear pinned choices")] = False,
    tmdb: Annotated[bool, typer.Option("--tmdb", help="Clear TMDB search cache")] = False,
    moves: Annotated[bool, typer.Option("--moves", help="Clear move log")] = False,
    all_tables: Annotated[bool, typer.Option("--all", help="Clear everything")] = False,
    cache_db: Annotated[Path, typer.Option("--cache-db")] = _DEFAULT_DB,
) -> None:
    """Delete rows from the cache. Requires at least one --pinned/--tmdb/--moves/--all flag."""
    if not any([pinned, tmdb, moves, all_tables]):
        err_console.print(
            "[bold red]Error:[/bold red] Specify at least one of --pinned, --tmdb, --moves, --all"
        )
        raise typer.Exit(1)
    cache = Cache(cache_db)
    deleted = cache.clear(
        pinned=pinned or all_tables,
        tmdb=tmdb or all_tables,
        moves=moves or all_tables,
    )
    cache.close()
    for table, count in deleted.items():
        console.print(f"  [green]✓[/green] {table}: deleted [bold]{count}[/bold] rows")


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
