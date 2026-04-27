# jelly-file-mngr

Organize a messy library of media rips into a [Jellyfin](https://jellyfin.org/)-compatible directory structure using [guessit](https://github.com/guessit-io/guessit) for filename parsing, [TMDB](https://www.themoviedb.org/) for metadata matching, and [AniList](https://anilist.co/) as an automatic fallback for anime titles.

**Dry-run by default.** Nothing is ever moved until you explicitly pass `--apply`.  
**Interactive by default.** When a match is ambiguous you are prompted to choose. Pass `--no-interactive` for automation.

---

## Features

### Multi-episode files
Files spanning multiple episodes (`Show.S03E01E02.mkv`) are renamed to Jellyfin's expected `S03E01-E02.mkv` format rather than silently dropping the second episode.

### Bare episode filenames
Files named after an episode title with no `S/E` marker (`Luck of the Fryrish.mkv`) no longer collapse every file in the folder to `S01E01.mkv`. In `--interactive` mode, after the show is matched on TMDB, the season episode list is fetched and shown so you can identify the correct episode by title.

### Rich destination filenames
By default episodes land as `S02E22.mkv`. Pass `--rich-names` to include the episode title, series name, and video quality in the filename:

```
S02E22-The Cave of Two Lovers-Avatar The Last Airbender-720p.mkv
```

Useful when people browse the SMB share directly rather than through Jellyfin. Jellyfin itself reads the `SxxExx` pattern and is happy with either format.

### Duplicate handling — `dedupe` subcommand
When two source files would land at the same destination (e.g. a 1080p and a 720p of the same episode), use the `dedupe` subcommand to resolve them. `organize` itself just skips duplicates with a hint pointing here.

```bash
# Interactive prompt per duplicate pair (default)
uv run jellyfiler dedupe /source /dest

# Auto-keep highest quality, quarantine losers (recoverable, cron-friendly)
uv run jellyfiler dedupe /source /dest --quarantine-duplicates --apply

# Auto-keep highest quality, PERMANENTLY DELETE losers (cron-friendly with double-flag safety)
uv run jellyfiler dedupe /source /dest --remove-duplicates --i-mean-it --apply
```

| Flag(s) | Behaviour | Reversible? |
|---|---|---|
| (none) interactive | Prompts per duplicate pair. Shows path + resolution + size for each candidate. Options: keep one, skip both, sticky "always highest", sticky "always highest + quarantine losers", **one-shot "delete losers + their parent dir"**. | Per choice |
| (none) `--no-interactive` | Skip both files in every duplicate pair. Safe default for unattended runs. | Yes |
| `--quarantine-duplicates` | Auto-keep highest quality, move losers to `dest/.junk/duplicates/`. Single flag — no extra confirmation since it's reversible. Cron-friendly. | Yes |
| `--remove-duplicates --i-mean-it` | Auto-keep highest quality, **PERMANENTLY DELETE** losers. The double-flag protects against accidents in cron jobs. | **No** |

Quality ranking: filename resolution tag (2160p > 1080p > 720p > 480p) is the primary key, file size on disk is the tiebreaker.

`--remove-duplicates` without `--i-mean-it` aborts with a big red warning — there is no "delete by accident" path.

### TMDB rate limiting
The TMDB client throttles outbound requests to **40 RPS** by default — comfortably below TMDB's documented 50 RPS cap on free API keys, with a buffer for clock drift and burstiness. If TMDB returns `429 Too Many Requests`, the client honours the `Retry-After` header and retries once. Configurable via `TmdbClient(rps=...)` if you have a paid plan with higher limits; set `rps=0` to disable.

### Parallel TMDB lookups (`--parallel N`, default 12)
Each TMDB call spends most of its time waiting on the network. By default jellyfiler fans lookups out across **12 worker threads** — comfortably under TMDB's 50 RPS cap and a good fit for any modern machine. Pass `-j 1` to force the old sequential behaviour, or `-j 16` to push closer to the rate limit on a fast connection.

```bash
uv run jellyfiler organize /source /dest --apply              # 12 workers (default)
uv run jellyfiler organize /source /dest -j 1 --apply         # sequential
uv run jellyfiler organize /source /dest -j 16 --apply        # push closer to RPS cap
```

Three-phase pipeline:
1. **Classify** — sequential, fast: cache check, junk filter, guessit parse, pinned-cache lookup. Interactive "missing title" prompt fires here if needed.
2. **Lookup** — parallel: TMDB → variant retries → AniList → AI fallback chain runs across N threads. The rate limiter is a global token bucket so all workers share the budget.
3. **Finalize** — sequential, on the main thread: ambiguous-match prompt, bare-episode prompt, plan + pin.

Output ordering is preserved — phase 3 walks classifications in input order, even though phase 2 completes futures out of order. The AI-disable interactive prompt is suppressed in parallel mode (worker threads can't share stdin); AI errors abort the run instead.

### Subtitle sidecars
After each video move, subtitle files sharing the same stem (`.srt`, `.ass`, `.vtt`, `.sub`, `.ssa`, `.sup`) are moved alongside and renamed to match the destination. Language codes are preserved: `episode.en.srt` → `S01E05.en.srt`.

Subtitles packed in a subdirectory next to the video (`Subs/`, `Subtitles/`, `English/`, etc.) are automatically discovered — not just files sitting directly beside the video.

### OVA routing to Season 00
OVAs / OADs / ONAs (Original Video / Animation / Net Animation — anime side stories) are auto-routed to **Season 00** (Jellyfin's Specials slot) so they don't collide with regular-season episode numbering. Detection works on the filename (`Show.OVA.01.mkv`) or the parent dir (`OVA/`). Files already tagged `S00E01` are left alone.

### Real-world title parsing
Battle-tested against ~350 real release names from a messy SMB library. The parser handles:

- **Spaced SxxExx**: `Show - S01 E01 - Title.mp4` (extra space between season/episode)
- **Three-digit episodes**: `[Judas] Hunter x Hunter (2011) - S01E001.mkv`
- **Dual numbering**: `American Dad! S07E01 (S08E01) Hot Water.mkv` → uses Fox numbering (TMDB-aligned)
- **Split-episode markers**: `S01E01a Downtown.mkv` / `S01E01b Eugene's Bike.mkv` — letter suffix preserved in destination so the two halves don't collide
- **Dash-separated movie subtitles**: `The Punisher - War Zone (2008).mkv` → `Punisher: War Zone` (combines `title + alternative_title` for movies only — TV folder noise stays out)
- **Year-range season folders**: `Season 1 (1994-95)` doesn't leak `year=1994` into the search
- **Anime intro/outro tracks**: `NCOP`, `NCED`, `Creditless_OP1`, `Non-Credit Ending` → quarantined as junk (never canonical episodes), but episode titles like "Endings Are Always..." or "Operation Ruthless" are NOT misclassified.

### Claude Haiku AI search fallback
When TMDB title-variant retries **and** the AniList anime fallback both miss, pass `--use-ai` to send the raw release directory and filename to `claude-haiku-4-5` for a clean search query. Requires `ANTHROPIC_API_KEY` — the flag is always opt-in so tokens are never spent without explicit intent. AniList runs first, so anime titles only reach the paid AI fallback when AniList itself can't find them.

Before scanning any files, `--use-ai` runs a preflight check: it verifies the key is set and that Haiku responds correctly. If either fails, the run aborts immediately with a clear error.

API errors during a run (bad key, quota, network) stop the run in `--no-interactive` mode. In interactive mode you are prompted to disable AI and continue without it.

The prompt is a single system instruction + the two raw strings, keeping token usage minimal across large libraries.

---

## What it does

Takes a source directory like:

```
movies/
  Blade.Runner.2049.2017.Hybrid.2160p.UHD.Blu-ray.Remux.HEVC.DV.HDR.TrueHD.7.1.Atmos-HDT.mkv
  Futurama.S12.1080p.x265-ELiTE/
    Futurama.S12E01.1080p.x265-ELiTE.mkv
    Futurama.S12E02.1080p.x265-ELiTE.mkv
```

And produces:

```
output/
  Blade Runner 2049 (2017)/
    Blade Runner 2049 (2017).mkv
  Futurama/
    Season 12/
      S12E01.mkv
      S12E02.mkv
  .junk/
    Futurama.S12.1080p.x265-ELiTE/
      Sample.mkv
      release.nfo
```

Junk files (samples, trailers, sidecar files, hash-named files, scene promo videos) are automatically detected and quarantined into a `.junk/` subdirectory in the destination — they never trigger a title-search prompt. The `.junk/` prefix is ignored by Jellyfin.

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- A free [TMDB API key](https://www.themoviedb.org/settings/api)
- AniList requires no API key — used automatically as a fallback for anime

---

## Installation

```bash
git clone https://github.com/Yornik/jelly-file-mngr.git
cd jelly-file-mngr
uv sync
```

---

## Usage

```bash
export TMDB_API_KEY=your_key_here
export ANTHROPIC_API_KEY=your_key_here  # required only when using --use-ai

# Dry run — shows what would happen, nothing is moved (default)
uv run jellyfiler organize /path/to/messy/movies /path/to/output

# Explicit dry-run flag (identical to the default, useful in scripts)
uv run jellyfiler organize /source /dest --dry-run

# Apply — actually move files (still interactive by default)
uv run jellyfiler organize /source /dest --apply

# Test against only the first 10 files before committing to a full run
uv run jellyfiler organize /source /dest --limit 10

# Re-process files already in the move log (undo a bad batch)
uv run jellyfiler organize /source /dest --force

# Suppress per-file output — show only the summary panel (good for large libraries)
uv run jellyfiler organize /source /dest --quiet

# Force all files to be treated as series episodes
uv run jellyfiler organize /source /dest --type episode

# Non-interactive — skip ambiguous matches instead of prompting (good for automation)
uv run jellyfiler organize /source /dest --no-interactive --apply

# Include episode title, series name, and quality in destination filenames
# e.g. S02E22-The Cave of Two Lovers-Avatar The Last Airbender-720p.mkv
uv run jellyfiler organize /source /dest --rich-names --apply

# Resolve duplicate destinations — see the dedupe subcommand below for details
uv run jellyfiler dedupe /source /dest --quarantine-duplicates --apply
uv run jellyfiler dedupe /source /dest --remove-duplicates --i-mean-it --apply

# Force sequential TMDB lookups (default is 12 parallel workers, throttled to 40 RPS)
uv run jellyfiler organize /source /dest -j 1 --apply

# Enable Claude Haiku AI fallback for titles that defeat all other parsing
# (requires ANTHROPIC_API_KEY — off by default to avoid unintentional spend)
uv run jellyfiler organize /source /dest --use-ai --apply

# Show version
uv run jellyfiler --version
```

### Debugging filenames

```bash
# Parse filenames with guessit without hitting TMDB — useful when a file is misidentified
uv run jellyfiler scan /path/to/source
```

Prints a table of what guessit detected for every media file in the directory:

| Filename | Type | Title | Year | S | E |
|---|---|---|---|---|---|
| Futurama.S12E01.1080p.x265.mkv | episode | Futurama | — | 12 | 1 |
| Blade.Runner.2049.2017.mkv | movie | Blade Runner 2049 | 2017 | — | — |

No TMDB calls are made. Use this to check why a file is being misidentified before running `organize`.

### In-place mode

Reorganize within the source directory itself — no separate destination needed.
Useful when `movies/` and `series/` are already separate and you just want clean structure inside each.

```bash
# Dry run in-place
uv run jellyfiler organize /media/movies --in-place

# Apply in-place
uv run jellyfiler organize /media/movies --in-place --apply

# Apply in-place and remove leftover empty release folders
uv run jellyfiler organize /media/movies --in-place --apply --cleanup-empty-dirs
```

Before:
```
movies/
  Blade.Runner.2049.2017.Hybrid.2160p.UHD.Blu-ray.Remux.HEVC.DV.HDR.TrueHD.7.1.Atmos-HDT.mkv
  Futurama.S12.1080p.x265-ELiTE/
    Futurama.S12E01.1080p.x265-ELiTE.mkv
```

After:
```
movies/
  Blade Runner 2049 (2017)/
    Blade Runner 2049 (2017).mkv
  Futurama/
    Season 12/
      S12E01.mkv
```

> **Note:** `--cleanup-empty-dirs` uses `rmdir` which only removes truly empty directories.
> Non-empty directories (e.g. a release folder that still has subtitle files) are left untouched.

---

## SQLite cache

jellyfiler keeps a cache at `~/.cache/jellyfiler/cache.db` (created automatically).

| What is cached | Benefit |
|---|---|
| TMDB search results | Running over 8 000 series files only hits TMDB once per unique title, not once per file. Persists across runs. |
| AniList search results | Same as TMDB — anime fallback queries are cached identically, no separate store needed. |
| Move history | Re-running the tool skips files already moved in a previous run. Safe to use as a resume mechanism if a run was interrupted. |

Override the location with `--cache-db /path/to/custom.db`.

### Cache management

```bash
# Show row counts for each cache table
uv run jellyfiler cache stats

# Remove a bad pinned match so the title is re-prompted on next run
uv run jellyfiler cache unpin "Futurama" --type episode
uv run jellyfiler cache unpin "Coco" --type movie --year 2017

# Selectively clear parts of the cache
uv run jellyfiler cache clear --pinned          # re-prompt all pinned titles
uv run jellyfiler cache clear --moves           # re-process already-moved files
uv run jellyfiler cache clear --tmdb            # force fresh TMDB lookups
uv run jellyfiler cache clear --all             # full reset
```

---

## Safety guarantees

- **Dry-run is the default.** You must pass `--apply` to move anything.
- **Nothing is overwritten.** If the destination already exists, the move is skipped.
- **Pre-flight checks run before the first file is touched.** Missing sources or unresolved problems abort the entire operation with a clear error — no partial moves.
- **Ambiguous matches are interactively resolved or skipped.** A wrong TMDB match is more dangerous than a skip. The tool defaults to asking rather than guessing wrong.
- **Junk is quarantined, not discarded.** Sample/trailer/sidecar/`NCOP` files go to `.junk/` in the destination, where they're easy to recover or delete by hand.
- **Deletion requires explicit double-flag opt-in.** The *only* way the tool ever deletes a file is `--remove-duplicates --i-mean-it`. Either flag alone aborts. The interactive duplicate prompt also has a one-shot **delete** option (`d`) that you must type per group — it's deliberately not sticky.

The interactive **delete-and-remove-parent-dir** option (`d` in the duplicate prompt) and the `--remove-duplicates --i-mean-it` flag pair are the only paths that ever `unlink()` user files. Everything else is a `move` or skip.

---

## Development

```bash
uv sync

# Lint
uv run ruff check .
uv run ruff format .

# Type check
uv run mypy src/

# Tests
uv run pytest
```

---

## License

MIT — see [LICENSE](LICENSE).
