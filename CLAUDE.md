# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv run jellyfiler <source> <dest>        # dry-run (default)
uv run jellyfiler <source> <dest> --apply

uv run pytest                            # all tests
uv run pytest tests/test_junk.py -v     # single test file
uv run pytest -k test_name              # single test by name

uv run ruff check .
uv run ruff format .
uv run mypy src/
```

`TMDB_API_KEY` env var must be set to run the CLI. Tests do not hit the network.

## Architecture

The pipeline runs entirely in `cli.py::organize()` and is strictly linear — each stage hands off to the next with no back-references:

```
scanner → guesser → [junk filter] → tmdb/anilist lookup → planner → executor
```

**Data flow through the main loop:**
1. `scanner.find_media_files(source)` — rglob for video extensions
2. `junk.is_junk(file)` — checked before anything else; junk files are moved to `dest/.junk/<relative-path>` and never enter the TMDB flow
3. `guesser.guess(file)` → `GuessedMedia` (title, year, season, episode, media_type via guessit)
4. Cache-first TMDB lookup via `cache.get_tmdb()` / `tmdb.search_movie()` / `tmdb.search_tv()`; AniList is a fallback for episodes that look like anime
5. `_resolve_match()` calls `tmdb.best_match()` then optionally `interactive.prompt_tmdb_match()` on ambiguity
6. `planner.plan_move()` → `PlannedMove` with the computed Jellyfin destination path
7. `executor.execute()` runs preflight checks then `shutil.move()` per file

**Key dataclasses** (`models.py`):
- `GuessedMedia` — guessit output normalised
- `TmdbMatch` — reused for both TMDB and AniList results (AniList integer ID stored as `tmdb_id`)
- `PlannedMove` — one per file; `skipped=True` means it won't be moved
- `Plan` — splits `PlannedMove` list into `moves` and `skipped`

**Jellyfin path conventions** (`planner.py`):
- Movie: `dest/Title (Year)/Title (Year).ext`
- Episode: `dest/Show Name/Season NN/SNNENN.ext`
- Junk quarantine: `dest/.junk/<relative-from-source>/filename` (dot-prefix so Jellyfin ignores it)

**SQLite cache** (`cache.py`, `~/.cache/jellyfiler/cache.db`):
- `tmdb_cache` — keyed on `(title.lower(), year, media_type)`; stores JSON-serialised `list[TmdbMatch]`
- `move_log` — records every successful move; checked at loop start to skip already-moved files

**Executor safety model** (`executor.py`): preflight checks run before any file is touched — missing source, existing destination, duplicate destinations all abort the entire run. No partial moves.

## Adding a new feature

- New filesystem mutations belong in `executor.py` or `junk.py` — not in `cli.py`.
- New persistent data needs a new column or table in `cache.py::_SCHEMA`.
- `cli.py` imports everything explicitly — new modules need wiring there.
- Coverage threshold is 30% (set in `pyproject.toml`). Running only a new test file in isolation will fail the coverage gate even if all tests pass — run `uv run pytest` to check overall coverage.
