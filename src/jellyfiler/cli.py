"""CLI entry point.

The two top-level operations are:

  * ``organize`` — scan SOURCE, match files against TMDB, plan moves, execute.
    Skips duplicates without touching them. Use ``dedupe`` to clean those up.
  * ``dedupe`` — same scan + match pipeline, but only acts on files that would
    collide on the same destination. Resolves duplicates per CLI flags
    (interactive prompt, --quarantine-duplicates, or --remove-duplicates --i-mean-it).

Both share the per-file pipeline via :func:`_process_one_file`. The shared bits
also include arg validation (:func:`_validate_in_place_args`), the AI preflight
check, the TMDB-search-with-fallbacks chain (:func:`_lookup_match_chain`), and
the post-execute cleanup (:func:`_apply_dedupe_actions`).
"""

import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TextColumn,
)
from rich.text import Text

from jellyfiler.ai_query import AiQueryError, preflight_check, suggest_search
from jellyfiler.anilist import looks_like_anime, search_anime
from jellyfiler.cache import _DEFAULT_DB, Cache
from jellyfiler.dedupe import (
    DuplicateChoice,
    PromptFn,
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
from jellyfiler.models import GuessedMedia, MediaType, Plan, PlannedMove
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


# ───────────────────────────────────────────────────────────────────────────
# Tiny pure helpers
# ───────────────────────────────────────────────────────────────────────────


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
    stripped = _strip_roman_suffix(title)
    if stripped != title:
        variants.append(stripped)
    if "&" in title:
        variants.append(title.replace("&", "and").replace("  ", " ").strip())
    spaced = _CAMEL_SPLIT.sub(" ", title)
    if spaced != title:
        variants.append(spaced)
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
    if interactive and matches:
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


# ───────────────────────────────────────────────────────────────────────────
# Pipeline context + per-file outcome
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class OrganizeContext:
    """Bag of pipeline state shared across the per-file processing.

    All the per-file branches in ``_process_one_file`` look up their dependencies
    here instead of taking ten parameters. Mutable state (``ai_disabled``) lives
    here too so the caller can see updates.

    ``parallel`` controls the TMDB-lookup worker pool size. 1 = sequential
    (default, identical to the pre-parallel behaviour); higher = use that many
    threads for the network-bound lookup chain. Phase 1 (file classification)
    and phase 3 (interactive resolution) always run on the main thread.
    """

    source: Path
    dest: Path
    tmdb: TmdbClient
    cache: Cache
    interactive: bool
    use_ai: bool
    forced_media_type: MediaType
    fancy_title: bool
    quiet: bool
    force: bool
    ai_disabled: bool = False  # toggled to True if the user opts out mid-run
    parallel: int = 1


@dataclass
class ClassifiedFile:
    """Phase-1 outcome — what we know about a file before any TMDB call.

    ``kind`` is one of:
        ``cached``        — already moved in a prior run, skip silently
        ``junk``          — junk filter matched, will be moved to .junk/
        ``skipped``       — pre-decided skip (unknown type, no title, ...);
                            ``move`` carries the skipped PlannedMove
        ``pinned``        — TMDB lookup short-circuits via cache.get_pinned();
                            ``move`` carries the resolved PlannedMove
        ``needs_lookup``  — phase-2 must run the TMDB chain on ``guessed``
    """

    file: Path
    kind: str
    guessed: GuessedMedia | None = None  # set for needs_lookup
    move: PlannedMove | None = None  # set for skipped / pinned


@dataclass
class FileResult:
    """Outcome of processing one file.

    ``kind`` is one of:
        ``planned``   — :attr:`move` is set (may be skipped=True)
        ``junk``      — file should be added to the junk list (no move)
        ``cached``    — already-moved file, skip silently
        ``tmdb_error`` — fatal lookup error; caller should break out of the loop
        ``ai_abort``  — user declined to disable AI after an error; abort run
    """

    kind: str
    move: PlannedMove | None = None
    error_msg: str = ""


# ───────────────────────────────────────────────────────────────────────────
# Argument validation
# ───────────────────────────────────────────────────────────────────────────


def _validate_in_place_args(
    in_place: bool,
    dest: Path | None,
    source: Path,
    cleanup_empty_dirs: bool,
) -> Path:
    """Resolve ``dest`` for in-place vs separate-dest mode and validate flags.

    Returns the effective destination directory. Raises typer.Exit on bad
    flag combinations.
    """
    if in_place:
        if dest is not None:
            err_console.print(
                "[bold red]Error:[/bold red] Cannot combine --in-place with a DEST argument."
            )
            raise typer.Exit(1)
        console.print(f"[bold yellow]IN-PLACE mode — reorganizing within {source}[/bold yellow]")
        dest = source
    elif dest is None:
        err_console.print("[bold red]Error:[/bold red] DEST is required unless --in-place is used.")
        raise typer.Exit(1)

    if cleanup_empty_dirs and not in_place:
        err_console.print(
            "[bold red]Error:[/bold red] --cleanup-empty-dirs only makes sense with --in-place."
        )
        raise typer.Exit(1)

    return dest


def _validate_dedupe_flags(
    remove_duplicates: bool,
    i_mean_it: bool,
    quarantine_duplicates: bool,
) -> None:
    """Run the safety gates for the dedupe flags. Raises typer.Exit on misuse."""
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
            "[bold red]Error:[/bold red] Cannot combine --quarantine-duplicates with "
            "--remove-duplicates. Quarantine moves losers to .junk/duplicates/ "
            "(recoverable); remove deletes them."
        )
        raise typer.Exit(1)


def _ai_preflight(use_ai: bool, quiet: bool) -> None:
    """Run the AI preflight check if --use-ai is on. Raises typer.Exit on failure."""
    if not use_ai:
        return
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


# ───────────────────────────────────────────────────────────────────────────
# TMDB lookup chain (TMDB → variants → AniList → AI fallback)
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class LookupResult:
    """Outcome of the TMDB+AniList+AI search chain for one file."""

    matches: list[TmdbMatch] = field(default_factory=list)
    search_title: str = ""
    status: str = "ok"  # "ok" | "tmdb_error" | "ai_abort"
    error_msg: str = ""


def _lookup_match_chain(
    guessed: GuessedMedia,
    file: Path,
    ctx: OrganizeContext,
    progress: Progress | None = None,
) -> LookupResult:
    """Run the full search chain. Updates ``ctx.ai_disabled`` if the user opts out."""
    cache_year = guessed.year if guessed.media_type == MediaType.MOVIE else None

    # Step 1: TMDB lookup with cache
    try:
        cached = ctx.cache.get_tmdb(guessed.title, cache_year, guessed.media_type)
        if cached is not None:
            matches = cached
        elif guessed.media_type == MediaType.MOVIE:
            matches = ctx.tmdb.search_movie(guessed.title, guessed.year)
            ctx.cache.set_tmdb(guessed.title, guessed.year, guessed.media_type, matches)
        else:
            matches = ctx.tmdb.search_tv(guessed.title, None)
            ctx.cache.set_tmdb(guessed.title, None, guessed.media_type, matches)
    except httpx.HTTPStatusError as exc:
        err_console.print(
            f"\n[bold red]TMDB error: {exc.response.status_code} "
            f"{exc.response.reason_phrase} — stopping.[/bold red]"
        )
        return LookupResult(status="tmdb_error", error_msg=str(exc))
    except Exception as exc:
        err_console.print(f"\n[bold red]TMDB error: {exc} — stopping.[/bold red]")
        return LookupResult(status="tmdb_error", error_msg=str(exc))

    # Step 2: Title variant retries
    search_title = guessed.title
    if not best_match(matches, guessed.title, guessed.year):
        for variant in _title_variants(guessed.title):
            try:
                retry = (
                    ctx.tmdb.search_movie(variant, guessed.year)
                    if guessed.media_type == MediaType.MOVIE
                    else ctx.tmdb.search_tv(variant, None)
                )
                if retry and best_match(retry, variant, guessed.year):
                    matches = retry
                    search_title = variant
                    ctx.cache.set_tmdb(variant, cache_year, guessed.media_type, retry)
                    break
            except Exception:
                pass

    # Step 3: AniList fallback for anime episodes
    if (
        not best_match(matches, search_title, guessed.year)
        and guessed.media_type == MediaType.EPISODE
        and looks_like_anime(file.name)
    ):
        try:
            al_cached = ctx.cache.get_tmdb(guessed.title, guessed.year, MediaType.EPISODE)
            if al_cached is None:
                al_matches = search_anime(guessed.title)
                ctx.cache.set_tmdb(guessed.title, guessed.year, MediaType.EPISODE, al_matches)
            else:
                al_matches = al_cached
            if al_matches:
                if not ctx.quiet:
                    console.print(f"[dim]TMDB missed '{guessed.title}' — trying AniList...[/dim]")
                matches = al_matches
                search_title = guessed.title
        except Exception as exc:
            console.print(f"[dim]AniList fallback failed for '{file.name}': {exc}[/dim]")

    # Step 4: AI fallback (paid)
    if ctx.use_ai and not ctx.ai_disabled and not best_match(matches, search_title, guessed.year):
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
                err_console.print(f"\n[bold red]Anthropic API error: {exc}[/bold red]")
                # Parallel mode: skip the interactive disable prompt — multiple
                # worker threads can't share stdin. Treat as fatal; user can
                # retry without --use-ai or with --parallel 1.
                if ctx.interactive and ctx.parallel <= 1:
                    if progress is not None:
                        progress.stop()
                    disable = typer.confirm("Disable AI and continue without it?")
                    if progress is not None:
                        progress.start()
                    if disable:
                        ctx.ai_disabled = True
                    else:
                        return LookupResult(
                            matches=matches,
                            search_title=search_title,
                            status="ai_abort",
                            error_msg=str(exc),
                        )
                else:
                    return LookupResult(
                        matches=matches,
                        search_title=search_title,
                        status="ai_abort",
                        error_msg=str(exc),
                    )
            else:
                if suggestion:
                    ai_title = str(suggestion.get("title", ""))
                    ai_year_raw = suggestion.get("year")
                    ai_year = int(ai_year_raw) if isinstance(ai_year_raw, (int, float)) else None
                    if ai_title and ai_title != search_title:
                        console.print(
                            f"[dim]AI query suggestion for '{guessed.title}': "
                            f"'{ai_title}' ({ai_year})[/dim]"
                        )
                        try:
                            ai_retry = (
                                ctx.tmdb.search_movie(ai_title, ai_year)
                                if guessed.media_type == MediaType.MOVIE
                                else ctx.tmdb.search_tv(ai_title, None)
                            )
                            if ai_retry:
                                matches = ai_retry
                                search_title = ai_title
                                ctx.cache.set_tmdb(
                                    ai_title, cache_year, guessed.media_type, ai_retry
                                )
                        except Exception:
                            pass

    return LookupResult(matches=matches, search_title=search_title, status="ok")


# ───────────────────────────────────────────────────────────────────────────
# Per-file processing
# ───────────────────────────────────────────────────────────────────────────


def _skipped_move(
    file: Path,
    dest: Path,
    media_type: MediaType,
    matched_title: str,
    reason: str,
    tmdb_id: int | None = None,
) -> PlannedMove:
    """Build a skipped PlannedMove with a clear reason."""
    return PlannedMove(
        source=file,
        destination=dest,
        media_type=media_type,
        tmdb_id=tmdb_id,
        matched_title=matched_title,
        confidence="low",
        skipped=True,
        skip_reason=reason,
    )


def _classify_file(
    file: Path,
    ctx: OrganizeContext,
    progress: Progress | None = None,
) -> ClassifiedFile:
    """Phase 1: fast, sequential classification.

    Runs the cheap parts (cache lookup, junk filter, guessit parse) and decides
    whether the file:
      * is already done / junk → no further work
      * has a pre-resolved skip reason → done with a skipped PlannedMove
      * has a pinned TMDB match → done with the resolved PlannedMove
      * needs the (slow, network-bound) TMDB chain in phase 2

    The interactive "missing title" prompt also fires here — it's the only
    interactive prompt that gates the lookup, so it must run before phase 2.
    """
    # 1. Cached skip
    if not ctx.force and ctx.cache.already_moved(file):
        if not ctx.quiet:
            console.print(f"[dim]SKIP (cached):[/dim] {file.name}")
        return ClassifiedFile(file=file, kind="cached")

    # 2. Junk filter
    if is_junk(file):
        if not ctx.quiet:
            console.print(f"[dim]JUNK:[/dim] {file.name}")
        return ClassifiedFile(file=file, kind="junk")

    # 3. Guess
    guessed = guess(file)
    if ctx.forced_media_type != MediaType.UNKNOWN:
        guessed.media_type = ctx.forced_media_type

    # 4. Unknown type — skipped move
    if guessed.media_type == MediaType.UNKNOWN:
        if not ctx.quiet:
            console.print(f"[yellow]SKIP (unknown type):[/yellow] {file.name}")
        return ClassifiedFile(
            file=file,
            kind="skipped",
            move=_skipped_move(
                file,
                ctx.dest,
                MediaType.UNKNOWN,
                guessed.title or file.name,
                "Could not determine media type — pass --type to force",
            ),
        )

    # 5. Missing title — interactive prompt or skip (interactive runs on main thread)
    if not guessed.title:
        if ctx.interactive:
            if progress is not None:
                progress.stop()
            manual = prompt_manual_title(file.name, "")
            if progress is not None:
                progress.start()
            if manual:
                guessed.title = manual
            else:
                return ClassifiedFile(
                    file=file,
                    kind="skipped",
                    move=_skipped_move(
                        file,
                        ctx.dest,
                        guessed.media_type,
                        file.name,
                        "User skipped — no title provided",
                    ),
                )
        else:
            if not ctx.quiet:
                console.print(f"[yellow]SKIP (no title parsed):[/yellow] {file.name}")
            return ClassifiedFile(
                file=file,
                kind="skipped",
                move=_skipped_move(
                    file,
                    ctx.dest,
                    guessed.media_type,
                    file.name,
                    "guessit could not extract a title — run with --interactive",
                ),
            )

    cache_year = guessed.year if guessed.media_type == MediaType.MOVIE else None

    # 6. Pinned cache hit
    pinned = ctx.cache.get_pinned(guessed.title, cache_year, guessed.media_type)
    if pinned:
        if not ctx.quiet:
            console.print(f"[dim]PINNED:[/dim] {guessed.title} → {pinned.title} ({pinned.year})")
        return ClassifiedFile(
            file=file,
            kind="pinned",
            guessed=guessed,
            move=plan_move(guessed, pinned, ctx.dest, file, fancy_title=ctx.fancy_title),
        )

    # 7. Needs the TMDB lookup chain
    return ClassifiedFile(file=file, kind="needs_lookup", guessed=guessed)


def _finalize_after_lookup(
    classified: ClassifiedFile,
    lookup: LookupResult,
    ctx: OrganizeContext,
    progress: Progress | None = None,
) -> FileResult:
    """Phase 3: turn (classified file + lookup result) into a FileResult.

    Runs match resolution (which may prompt interactively for ambiguous matches),
    the bare-episode prompt, and the final ``plan_move`` + ``set_pinned`` call.
    Always runs on the main thread so interactive prompts work.
    """
    file = classified.file
    guessed = classified.guessed
    assert guessed is not None  # invariant: needs_lookup → guessed is set

    if lookup.status == "tmdb_error":
        return FileResult(kind="tmdb_error", error_msg=lookup.error_msg)
    if lookup.status == "ai_abort":
        return FileResult(kind="ai_abort", error_msg=lookup.error_msg)

    cache_year = guessed.year if guessed.media_type == MediaType.MOVIE else None

    # Resolve match (interactive prompt if ambiguous)
    if ctx.interactive and progress is not None:
        progress.stop()
    match = _resolve_match(
        file,
        lookup.search_title,
        guessed.year,
        lookup.matches,
        guessed.media_type,
        ctx.interactive,
    )
    if ctx.interactive and progress is not None:
        progress.start()

    # Non-interactive ambiguous → skip
    if not match and not ctx.interactive and lookup.matches:
        if not ctx.quiet:
            console.print(
                f"[yellow]SKIP (ambiguous):[/yellow] '{guessed.title}' — "
                f"{len(lookup.matches)} TMDB results, none matched confidently. "
                "Run with --interactive to pick manually."
            )
        return FileResult(
            kind="planned",
            move=_skipped_move(
                file,
                ctx.dest,
                guessed.media_type,
                guessed.title,
                f"Ambiguous: {len(lookup.matches)} results, no confident match. Use --interactive.",
            ),
        )

    # Bare-episode interactive prompt
    if (
        match is not None
        and guessed.media_type == MediaType.EPISODE
        and guessed.episode is None
        and ctx.interactive
    ):
        if progress is not None:
            progress.stop()
        try:
            season_num = guessed.season or 1
            episodes = ctx.tmdb.get_season_episodes(match.tmdb_id, season_num)
            if episodes:
                picked = prompt_episode_number(file.name, episodes)
                if picked is not None:
                    guessed.episode = picked
        except Exception as exc:
            console.print(f"[dim]Could not fetch episode list: {exc}[/dim]")
        if progress is not None:
            progress.start()

    # Plan + pin
    move = plan_move(guessed, match, ctx.dest, file, fancy_title=ctx.fancy_title)
    if match:
        ctx.cache.set_pinned(lookup.search_title, cache_year, guessed.media_type, match)
    return FileResult(kind="planned", move=move)


def _process_one_file(
    file: Path,
    ctx: OrganizeContext,
    progress: Progress | None = None,
) -> FileResult:
    """Sequential per-file processing: phase 1 → phase 2 → phase 3.

    Used by the sequential pipeline runner. The parallel runner calls phase 1
    and phase 3 directly so it can fan phase 2 out across worker threads.
    """
    classified = _classify_file(file, ctx, progress=progress)
    if classified.kind == "cached":
        return FileResult(kind="cached")
    if classified.kind == "junk":
        return FileResult(kind="junk")
    if classified.kind in ("skipped", "pinned"):
        assert classified.move is not None
        return FileResult(kind="planned", move=classified.move)
    # needs_lookup
    assert classified.guessed is not None
    lookup = _lookup_match_chain(classified.guessed, file, ctx, progress=progress)
    return _finalize_after_lookup(classified, lookup, ctx, progress=progress)


# ───────────────────────────────────────────────────────────────────────────
# Pipeline runner (the loop) — used by both organize and dedupe
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class PipelineResult:
    """Aggregated outcomes from running the per-file loop over many files."""

    planned_moves: list[PlannedMove] = field(default_factory=list)
    junk_files: list[Path] = field(default_factory=list)
    tmdb_errors: int = 0
    aborted: bool = False  # set when caller hit ai_abort or fatal tmdb error


def _run_pipeline(files: list[Path], ctx: OrganizeContext) -> PipelineResult:
    """Run :func:`_process_one_file` over each input file with a progress bar.

    Dispatches to :func:`_run_pipeline_parallel` when ``ctx.parallel > 1``.
    """
    if ctx.parallel > 1:
        return _run_pipeline_parallel(files, ctx)

    out = PipelineResult()
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

            result = _process_one_file(file, ctx, progress=progress)
            if result.kind == "junk":
                out.junk_files.append(file)
            elif result.kind == "planned" and result.move is not None:
                out.planned_moves.append(result.move)
            elif result.kind == "tmdb_error":
                out.tmdb_errors += 1
                out.aborted = True
                break
            elif result.kind == "ai_abort":
                err_console.print("[bold red]Stopping.[/bold red]")
                out.aborted = True
                break
            # 'cached' — silently skip
            progress.advance(task)
    return out


class _ActiveCounter:
    """Thread-safe live count of workers currently inside the lookup chain.

    Used by :class:`_ActiveWorkersColumn` to render a live "⚙ N/M" indicator
    in the progress bar so the operator can see whether the parallel pool
    is saturated or idling.
    """

    __slots__ = ("_n", "_lock")

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def inc(self) -> None:
        with self._lock:
            self._n += 1

    def dec(self) -> None:
        with self._lock:
            self._n -= 1

    def get(self) -> int:
        with self._lock:
            return self._n


class _ActiveWorkersColumn(ProgressColumn):
    """Renders ⚙ <active>/<max> in the progress bar — refreshes ~10x/sec."""

    def __init__(self, counter: _ActiveCounter, max_workers: int) -> None:
        super().__init__()
        self._counter = counter
        self._max = max_workers

    def render(self, task: Task) -> Text:
        return Text(f"⚙ {self._counter.get()}/{self._max}", style="cyan")


def _tracked_lookup(
    counter: _ActiveCounter,
    guessed: GuessedMedia,
    file: Path,
    ctx: OrganizeContext,
) -> LookupResult:
    """Wrap ``_lookup_match_chain`` so we can count threads in flight."""
    counter.inc()
    try:
        return _lookup_match_chain(guessed, file, ctx, progress=None)
    finally:
        counter.dec()


def _run_pipeline_parallel(files: list[Path], ctx: OrganizeContext) -> PipelineResult:
    """Three-phase pipeline that parallelises the TMDB lookup phase.

    Phase 1: classify every file sequentially (cache, junk, guess, pinned, ...).
    Phase 2: fan TMDB lookups out across ``ctx.parallel`` worker threads.
    Phase 3: walk back through the classifications in order, attaching the
             lookup result and running the interactive resolution path.

    Output ordering is preserved by walking ``classifications`` in input order
    in phase 3 — even though phase 2 completes futures out of order.

    Phase 2 shows a live ⚙ <active>/<max> worker count in the progress bar
    so you can see whether the rate limiter is saturating the pool.
    """
    from concurrent.futures import ThreadPoolExecutor

    out = PipelineResult()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
    ) as progress:
        # ── Phase 1: classify ──────────────────────────────────────────────
        classify_task = progress.add_task("Classifying files...", total=len(files))
        classifications: list[ClassifiedFile] = []
        for file in files:
            label = file.name if len(file.name) <= 55 else file.name[:52] + "..."
            progress.update(classify_task, description=f"[cyan]{label}[/cyan]")
            classifications.append(_classify_file(file, ctx, progress=progress))
            progress.advance(classify_task)

        # ── Phase 2: parallel TMDB lookups ──────────────────────────────────
        needs_lookup = [cf for cf in classifications if cf.kind == "needs_lookup"]
        lookup_results: dict[Path, LookupResult] = {}

        if needs_lookup:
            workers = max(1, ctx.parallel)
            active = _ActiveCounter()
            # Splice a live "⚙ N/M workers" column into the existing progress
            # bar for the duration of phase 2. Rich refreshes ~10×/sec so the
            # count updates in real time as workers enter/leave the lookup chain.
            workers_col = _ActiveWorkersColumn(active, workers)
            progress.columns = (*progress.columns, workers_col)
            lookup_task = progress.add_task(
                f"TMDB lookups (×{workers} workers)...", total=len(needs_lookup)
            )
            try:
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tmdb") as ex:
                    future_to_cf: dict[Any, ClassifiedFile] = {}
                    for cf in needs_lookup:
                        assert cf.guessed is not None  # invariant of needs_lookup
                        future_to_cf[
                            ex.submit(_tracked_lookup, active, cf.guessed, cf.file, ctx)
                        ] = cf
                    from concurrent.futures import as_completed

                    for fut in as_completed(future_to_cf):
                        cf = future_to_cf[fut]
                        try:
                            lookup_results[cf.file] = fut.result()
                        except Exception as exc:
                            # A worker raised — record as tmdb_error so phase 3 aborts cleanly.
                            lookup_results[cf.file] = LookupResult(
                                status="tmdb_error", error_msg=str(exc)
                            )
                        progress.advance(lookup_task)
            finally:
                # Drop the worker column when phase 2 ends so phase 3's bar
                # doesn't show a stale "0/N" indicator.
                progress.columns = tuple(c for c in progress.columns if c is not workers_col)

        # ── Phase 3: finalize in original order ────────────────────────────
        finalize_task = progress.add_task("Finalizing matches...", total=len(classifications))
        for cf in classifications:
            progress.advance(finalize_task)
            if cf.kind == "cached":
                continue
            if cf.kind == "junk":
                out.junk_files.append(cf.file)
                continue
            if cf.kind in ("skipped", "pinned"):
                assert cf.move is not None
                out.planned_moves.append(cf.move)
                continue
            # needs_lookup
            lookup = lookup_results[cf.file]
            result = _finalize_after_lookup(cf, lookup, ctx, progress=progress)
            if result.kind == "tmdb_error":
                out.tmdb_errors += 1
                out.aborted = True
                break
            if result.kind == "ai_abort":
                err_console.print("[bold red]Stopping.[/bold red]")
                out.aborted = True
                break
            if result.kind == "planned" and result.move is not None:
                out.planned_moves.append(result.move)

    return out


# ───────────────────────────────────────────────────────────────────────────
# Dedupe action helpers (delete / quarantine / remove dirs)
# ───────────────────────────────────────────────────────────────────────────


def _apply_dedupe_actions(
    losers_to_delete: list[PlannedMove],
    losers_to_quarantine: list[PlannedMove],
    dirs_to_remove: set[Path],
    source: Path,
    dest: Path,
) -> None:
    """Apply the post-execute file actions for the dedupe pass.

    Quarantine first (move → .junk/duplicates/), then delete loser files,
    then rmtree the marked parent directories. Each step swallows individual
    errors so one bad file doesn't abort the whole cleanup.
    """
    for loser in losers_to_quarantine:
        qpath = quarantine_path(loser, source, dest)
        try:
            qpath.parent.mkdir(parents=True, exist_ok=True)
            if qpath.exists():
                console.print(f"[dim]  quarantine target exists, skipping: {qpath}[/dim]")
                continue
            shutil.move(str(loser.source), str(qpath))
            console.print(f"[dim]  duplicate → {qpath}[/dim]")
        except Exception as exc:
            err_console.print(f"[yellow]Could not quarantine {loser.source}: {exc}[/yellow]")

    for loser in losers_to_delete:
        try:
            if loser.source.exists():
                loser.source.unlink()
                console.print(f"[red]  deleted duplicate: {loser.source}[/red]")
        except Exception as exc:
            err_console.print(f"[yellow]Could not delete {loser.source}: {exc}[/yellow]")

    for d in dirs_to_remove:
        try:
            if d.exists():
                shutil.rmtree(d)
                console.print(f"[red]  removed directory: {d}[/red]")
        except Exception as exc:
            err_console.print(f"[yellow]Could not remove {d}: {exc}[/yellow]")


def _resolve_dedupe(
    plan: Plan,
    *,
    interactive: bool,
    quarantine_duplicates: bool,
    remove_duplicates: bool,
    quiet: bool,
    dest: Path,
) -> tuple[Plan, list[PlannedMove], list[PlannedMove], set[Path]]:
    """Find duplicate destinations in *plan* and return the resolution plan.

    Returns (cleaned_plan, losers_to_delete, losers_to_quarantine, dirs_to_remove).
    Empty inputs and empty groups both return the original plan unchanged.
    """
    duplicate_groups = find_duplicate_groups(plan.moves)
    if not duplicate_groups:
        return plan, [], [], set()

    if not quiet:
        console.print(
            f"\n[bold yellow]Duplicate destinations detected:[/bold yellow] "
            f"{len(duplicate_groups)} group(s) — resolving..."
        )

    prompt_fn: PromptFn | None
    run_interactive: bool
    if quarantine_duplicates:

        def _quarantine_prompt(_group: list[PlannedMove]) -> DuplicateChoice:
            return DuplicateChoice(DuplicateChoice.ALWAYS_QUARANTINE)

        prompt_fn = _quarantine_prompt
        run_interactive = True
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

    if not quiet:
        if losers_to_delete:
            console.print(
                f"[bold red]{len(losers_to_delete)} duplicate loser(s) "
                "will be PERMANENTLY DELETED[/bold red]"
            )
        if dirs_to_remove:
            label = "directory" if len(dirs_to_remove) == 1 else "directories"
            console.print(
                f"[bold red]{len(dirs_to_remove)} parent {label} "
                "will also be removed (per user choice)[/bold red]"
            )
        if losers_to_quarantine:
            console.print(
                f"[yellow]{len(losers_to_quarantine)} duplicate loser(s) will be quarantined to "
                f"{dest / '.junk' / 'duplicates'}[/yellow]"
            )

    return plan, losers_to_delete, losers_to_quarantine, dirs_to_remove


# ───────────────────────────────────────────────────────────────────────────
# Junk + cleanup helpers
# ───────────────────────────────────────────────────────────────────────────


def _handle_junk_files(
    junk_files: list[Path],
    source: Path,
    dest: Path,
    dry_run: bool,
) -> int:
    """Report junk files and (when not dry-run) move them to dest/.junk/. Returns total bytes."""
    junk_bytes = sum(f.stat().st_size for f in junk_files if f.exists())
    if junk_files:
        report_junk(junk_files, source, dest, dry_run)
        if not dry_run:
            _moved, failed = move_junk(junk_files, source, dest)
            if failed:
                err_console.print(f"[yellow]{failed} junk file(s) could not be moved.[/yellow]")
    return junk_bytes


def _remove_empty_dirs(root: Path) -> None:
    """Recursively remove empty directories under root (but not root itself)."""
    removed = 0
    for dirpath in sorted(root.rglob("*"), reverse=True):
        if dirpath == root:
            continue
        if dirpath.is_dir():
            try:
                dirpath.rmdir()
                console.print(f"[dim]Removed empty dir: {dirpath}[/dim]")
                removed += 1
            except OSError:
                pass
    if removed:
        console.print(
            f"[dim]Cleaned up {removed} empty director{'y' if removed == 1 else 'ies'}.[/dim]"
        )


# ═══════════════════════════════════════════════════════════════════════════
# organize subcommand
# ═══════════════════════════════════════════════════════════════════════════


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
            help="Remove empty source directories after moving (only with --in-place --apply).",
        ),
    ] = False,
    fancy_title: Annotated[
        bool,
        typer.Option(
            "--fancy-title/--slim-title",
            help=(
                "Fancy (default): S01E01-Episode Title-Show Name-720p.ext. "
                "Slim: S01E01.ext. Fancy filenames disambiguate multi-segment "
                "episode slots (e.g. Animaniacs) that would otherwise collide."
            ),
        ),
    ] = True,
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
            help="Enable Claude Haiku AI fallback (requires ANTHROPIC_API_KEY).",
        ),
    ] = False,
    parallel: Annotated[
        int,
        typer.Option(
            "--parallel",
            "-j",
            help=(
                "Number of parallel TMDB lookup workers. Default 40 — fully "
                "saturates TMDB's 50 RPS cap even on slow-API days. Workers "
                "share the global rate limiter, so going higher costs only a "
                "few KB of stack per thread. Pass 1 to force sequential mode."
            ),
        ),
    ] = 40,
) -> None:
    """Scan SOURCE, match against TMDB, and organize into DEST.

    Files that would collide on the same destination are skipped silently —
    use the ``dedupe`` subcommand to clean those up.
    """
    if parallel < 1:
        err_console.print("[bold red]Error:[/bold red] --parallel must be >= 1.")
        raise typer.Exit(1)
    dest = _validate_in_place_args(in_place, dest, source, cleanup_empty_dirs)
    dry_run = not apply or dry_run_flag
    _ai_preflight(use_ai, quiet)

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

    cache = Cache(cache_db)
    ctx = OrganizeContext(
        source=source,
        dest=dest,
        tmdb=tmdb,
        cache=cache,
        interactive=interactive,
        use_ai=use_ai,
        forced_media_type=media_type,
        fancy_title=fancy_title,
        quiet=quiet,
        force=force,
        parallel=parallel,
    )

    pipeline = _run_pipeline(files, ctx)

    junk_bytes = _handle_junk_files(pipeline.junk_files, source, dest, dry_run)
    plan = build_plan(pipeline.planned_moves)

    # `organize` skips duplicates silently — the user runs `dedupe` to clean them up.
    duplicate_groups = find_duplicate_groups(plan.moves)
    if duplicate_groups and not quiet:
        console.print(
            f"\n[yellow]{len(duplicate_groups)} duplicate destination group(s) detected — "
            "skipping both files in each pair. Use 'jellyfiler dedupe' to resolve.[/yellow]"
        )
    if duplicate_groups:
        plan, _del, _quar, _dirs = _resolve_dedupe(
            plan,
            interactive=False,  # force skip-both behavior in organize
            quarantine_duplicates=False,
            remove_duplicates=False,
            quiet=quiet,
            dest=dest,
        )

    try:
        execute(plan, dry_run=dry_run, cache=cache, source_root=source)
    except ExecutionError as exc:
        err_console.print(f"\n[bold red]{exc}[/bold red]")
        raise typer.Exit(1) from exc

    _print_summary(
        planned=len(plan.moves),
        skipped=len(plan.skipped),
        junk_count=len(pipeline.junk_files),
        junk_bytes=junk_bytes,
        tmdb_errors=pipeline.tmdb_errors,
        dry_run=dry_run,
    )

    if pipeline.tmdb_errors:
        err_console.print(
            f"\n[yellow]{pipeline.tmdb_errors} TMDB error(s) occurred — see above.[/yellow]"
        )
        raise typer.Exit(1)

    if in_place and apply and cleanup_empty_dirs and not dry_run:
        _remove_empty_dirs(source)


# ═══════════════════════════════════════════════════════════════════════════
# dedupe subcommand
# ═══════════════════════════════════════════════════════════════════════════


@app.command()
def dedupe(
    source: Annotated[Path, typer.Argument(help="Source directory containing media files")],
    dest: Annotated[
        Path,
        typer.Argument(help="Destination root — used to compute would-be paths for grouping."),
    ],
    interactive: Annotated[
        bool,
        typer.Option(
            "--interactive/--no-interactive",
            help="Prompt per duplicate group (default: on). Off + no flag = skip both.",
        ),
    ] = True,
    quarantine_duplicates: Annotated[
        bool,
        typer.Option(
            "--quarantine-duplicates",
            help="Auto-keep highest quality, move losers to dest/.junk/duplicates/ (recoverable).",
        ),
    ] = False,
    remove_duplicates: Annotated[
        bool,
        typer.Option(
            "--remove-duplicates",
            help="Auto-keep highest quality, PERMANENTLY DELETE losers. Requires --i-mean-it.",
        ),
    ] = False,
    i_mean_it: Annotated[
        bool,
        typer.Option(
            "--i-mean-it",
            help="Required confirmation alongside --remove-duplicates. Without this, aborts.",
        ),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually act on duplicates. Without this, dry-run only."),
    ] = False,
    cache_db: Annotated[
        Path, typer.Option("--cache-db", help="Path to the SQLite cache database.")
    ] = _DEFAULT_DB,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress per-file output; show summary only.")
    ] = False,
    parallel: Annotated[
        int,
        typer.Option(
            "--parallel",
            "-j",
            help=(
                "Number of parallel TMDB lookup workers. Default 40 — saturates "
                "TMDB's rate cap even on slow-API days. Pass 1 to force sequential."
            ),
        ),
    ] = 40,
) -> None:
    """Find and resolve duplicate-destination files in SOURCE.

    Two source files matching the same TMDB show/episode would land at the same
    DEST path. This command finds those groups and applies a resolution
    (interactive prompt, quarantine, or delete). Winning copies stay in SOURCE
    untouched — use ``organize`` afterwards to move them to DEST.
    """
    if parallel < 1:
        err_console.print("[bold red]Error:[/bold red] --parallel must be >= 1.")
        raise typer.Exit(1)
    _validate_dedupe_flags(remove_duplicates, i_mean_it, quarantine_duplicates)
    dry_run = not apply

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

    cache = Cache(cache_db)
    ctx = OrganizeContext(
        source=source,
        dest=dest,
        tmdb=tmdb,
        cache=cache,
        interactive=interactive,
        use_ai=False,
        forced_media_type=MediaType.UNKNOWN,
        fancy_title=False,
        quiet=quiet,
        force=True,  # in dedupe we don't skip already-moved files (we want to find dupes)
        parallel=parallel,
    )

    pipeline = _run_pipeline(files, ctx)
    plan = build_plan(pipeline.planned_moves)

    plan, losers_to_delete, losers_to_quarantine, dirs_to_remove = _resolve_dedupe(
        plan,
        interactive=interactive,
        quarantine_duplicates=quarantine_duplicates,
        remove_duplicates=remove_duplicates,
        quiet=quiet,
        dest=dest,
    )

    if not (losers_to_delete or losers_to_quarantine):
        if not quiet:
            console.print("[green]No duplicates to resolve.[/green]")
        raise typer.Exit(0)

    if dry_run:
        console.print(
            f"\n[bold cyan]DRY RUN[/bold cyan] — pass --apply to actually delete/quarantine. "
            f"Would resolve {len(losers_to_delete) + len(losers_to_quarantine)} loser(s)."
        )
        raise typer.Exit(0)

    _apply_dedupe_actions(losers_to_delete, losers_to_quarantine, dirs_to_remove, source, dest)
    if not quiet:
        console.print(
            f"\n[bold green]Done.[/bold green] "
            f"Resolved {len(losers_to_delete) + len(losers_to_quarantine)} duplicate(s)."
        )


# ═══════════════════════════════════════════════════════════════════════════
# scan subcommand
# ═══════════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════════
# Cache subcommands
# ═══════════════════════════════════════════════════════════════════════════


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
