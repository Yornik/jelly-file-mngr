# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv run jellyfiler organize <source> <dest>        # dry-run (default)
uv run jellyfiler organize <source> <dest> --apply

uv run pytest                            # all tests
uv run pytest tests/test_junk.py -v     # single test file
uv run pytest -k test_name              # single test by name

uv run ruff check .                      # lint (run before every commit)
uv run ruff check . --fix                # auto-fix fixable issues (import order etc.)
uv run ruff format .                     # format (run before every commit)
uv run ruff format --check .             # check formatting without changing files
uv run mypy src/
```

**Before committing:** always run `uv run ruff check . && uv run ruff format .` — ruff enforces import ordering (I001) and formatting; both must be clean before committing.

`TMDB_API_KEY` env var must be set to run the CLI. Tests do not hit the network.

## Architecture

The pipeline runs entirely in `cli.py::organize()` and is strictly linear — each stage hands off to the next with no back-references:

```
scanner → guesser → [junk filter] → pinned-cache check → tmdb/anilist lookup → planner → executor
```

**Data flow through the main loop:**
1. `scanner.find_media_files(source)` — rglob for video extensions
2. `junk.is_junk(file)` — checked before anything else; junk files are moved to `dest/.junk/<relative-path>` and never enter the TMDB flow
3. `guesser.guess(file)` → `GuessedMedia` (title, year, season, episode, media_type via guessit + parent-dir fallback)
4. `cache.get_pinned()` — if a user previously confirmed a match interactively, use it directly and skip TMDB entirely
5. Cache-first TMDB lookup via `cache.get_tmdb()` / `tmdb.search_movie()` / `tmdb.search_tv()`; AniList is a fallback for episodes that look like anime
6. Title variant retries (Roman numeral strip, `&`→`and`, word segmentation via wordninja) when `best_match` fails
7. `_resolve_match()` calls `tmdb.best_match()` then optionally `interactive.prompt_tmdb_match()` on ambiguity; confirmed match is saved via `cache.set_pinned()`
8. `planner.plan_move()` → `PlannedMove` with the computed Jellyfin destination path
9. `executor.execute()` runs preflight checks then `shutil.move()` per file

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
- `tmdb_cache` — keyed on `(title.lower(), year, media_type)`; stores JSON-serialised `list[TmdbMatch]`. TV shows always use `year=None` as the key (TMDB's `first_air_date_year` is the premiere year, not the season year).
- `tmdb_pinned` — keyed on `(title.lower(), year, media_type)`; stores the single `TmdbMatch` the user confirmed. Checked before TMDB lookup — if present, skips API call and prompt entirely.
- `move_log` — records every successful move; checked at loop start to skip already-moved files

**Executor safety model** (`executor.py`): preflight checks run before any file is touched — missing source, existing destination, duplicate destinations all abort the entire run. No partial moves.

## Known library state (as of Apr 2026)

**SMB share:** `/mnt/smbshare/shared_evreyone/` — mounted with `uid=0` by default (needs remount with `uid=1000` to write from WSL). Mount command:
```bash
sudo umount /mnt/smbshare
sudo mount -t cifs //192.168.1.42/lilnasx /mnt/smbshare -o username=yornik,uid=1000,gid=1000,file_mode=0755,dir_mode=0755
```


## Title parsing quirks to know about

- **All-caps titles** (`DANNY PHANTOM`) — normalized to title case in `_clean_title` before TMDB search
- **Sort prefixes** (`b. Superman II`) — lowercase consonant prefix stripped in `_clean_title`
- **Quality residue** (`ghostbusters 720bd`) — trailing `\d{3,4}[bBpP]` stripped in `_clean_title`
- **Roman numeral suffix** (`Superman I`) — stripped in `_title_variants` retry; only fires when first search fails
- **Ampersand** (`Superman & Batman`) — retried as `and` in `_title_variants`
- **Run-together words** (`wonderwoman`) — wordninja splits single all-lowercase words in `_title_variants`
- **Accented titles** (`Pokémon`) — `_norm()` in `tmdb.best_match` strips combining characters before comparison
- **TV year filter** — never passed to `search_tv()`; season folder years (e.g. `Season 2 (2005-06)`) don't match TMDB's `first_air_date_year`

## Adding a new feature

- New filesystem mutations belong in `executor.py` or `junk.py` — not in `cli.py`.
- New persistent data needs a new column or table in `cache.py::_SCHEMA`.
- `cli.py` imports everything explicitly — new modules need wiring there.
- Coverage threshold is 30% (set in `pyproject.toml`). Running only a new test file in isolation will fail the coverage gate even if all tests pass — run `uv run pytest` to check overall coverage.
- Use `pytest` fixture pattern for `Cache` tests (yield + close) to avoid `ResourceWarning` on unclosed SQLite connections.
