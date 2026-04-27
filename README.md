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
| `--quarantine-duplicates` | Auto-keep highest quality, move losers to `dest/.aside/duplicates/`. Single flag — no extra confirmation since it's reversible. Cron-friendly. | Yes |
| `--remove-duplicates --i-mean-it` | Auto-keep highest quality, **PERMANENTLY DELETE** losers. The double-flag protects against accidents in cron jobs. | **No** |

Quality ranking: filename resolution tag (2160p > 1080p > 720p > 480p) is the primary key, file size on disk is the tiebreaker.

`--remove-duplicates` without `--i-mean-it` aborts with a big red warning — there is no "delete by accident" path.

### TMDB rate limiting
The TMDB client throttles outbound requests to **40 RPS** by default — comfortably below TMDB's documented 50 RPS cap on free API keys, with a buffer for clock drift and burstiness. If TMDB returns `429 Too Many Requests`, the client honours the `Retry-After` header and retries once. Configurable via `TmdbClient(rps=...)` if you have a paid plan with higher limits; set `rps=0` to disable.

### Parallel TMDB lookups (`--parallel N`, default 40)
Each TMDB call spends most of its time waiting on the network. By default jellyfiler fans lookups out across **40 worker threads** — enough to fully saturate TMDB's 50 RPS cap even on slow-API days. Workers share the global rate limiter, so going higher costs only a few KB of stack per thread; the actual outbound rate is always capped. Pass `-j 1` to force sequential mode for debugging.

```bash
uv run jellyfiler organize /source /dest --apply              # 40 workers (default)
uv run jellyfiler organize /source /dest -j 1 --apply         # sequential (debug)
```

Three-phase pipeline:
1. **Classify** — sequential, fast: cache check, aside filter (extras/samples/etc.), guessit parse, pinned-cache lookup. Interactive "missing title" prompt fires here if needed.
2. **Lookup** — parallel: TMDB → variant retries → AniList → AI fallback chain runs across N threads. The rate limiter is a global token bucket so all workers share the budget.
3. **Finalize** — sequential, on the main thread: ambiguous-match prompt, bare-episode prompt, plan + pin.

The progress bar gains a live `⚙ <active>/<max>` column during phase 2 so you can see whether the pool is saturated (active = max) or starving (active << max, usually means TMDB is responding fast and the rate limiter is the bottleneck — exactly what we want).

Output ordering is preserved — phase 3 walks classifications in input order, even though phase 2 completes futures out of order. The AI-disable interactive prompt is suppressed in parallel mode (worker threads can't share stdin); AI errors abort the run instead.

### Truncated plan output (`--full-plan` to opt out)
On a library with thousands of files, the move/skip table would otherwise be unusable. By default each section caps at 50 rows with a `… and N more` footer. Pass `--full-plan` to dump everything (e.g. when piping to `less`).

### Structured event log (`--log <path>`)
Append-only JSON-lines log of every classification, lookup, plan decision, dedupe action, and move:

```bash
uv run jellyfiler organize /source /dest --log run.jsonl --apply
jq 'select(.event == "match_skipped")' run.jsonl | head
jq 'select(.level == "error")' run.jsonl
```

One event per line. Fields include `ts` (UTC ISO-8601), `level` (debug/info/warning/error), `event` name, plus event-specific fields like `file`, `tmdb_id`, `confidence`, `reason`. Writes are thread-safe under `--parallel`. Events emitted: `run_started`, `classify_cached`, `classify_aside` (with `aside_kind`), `classify_pinned`, `match_resolved`, `match_skipped`, `duplicate_groups_detected`, `dedupe_will_delete`, `dedupe_will_quarantine`, `aside_jellyfin_extras`, `aside_aside_pile`, `aside_discarded`, `run_finished`.

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

### Claude Haiku AI fallbacks (`--use-ai`)
A single opt-in flag drives **two** Haiku-backed fallbacks. Both fire only when the cheap pattern/regex/TMDB chain has already exhausted itself, both cache aggressively, and both swallow network errors so a transient failure doesn't abort the run.

**1. TMDB search query suggestion** — when TMDB title-variant retries **and** the AniList anime fallback both miss, the raw release directory + filename go to Haiku for a clean search query. AniList runs first, so anime titles only reach the paid AI fallback when AniList itself can't find them.

**2. Aside (extras) classification** — when pattern-based `classify_aside` misses an unfamiliar parent dir name (`Bonusy/` Polish, `Doplnki/` Czech, ad-hoc `Bonus-Materials/`, etc.) **and** TMDB can't match it as main media, Haiku reads parent-dir + filename together and picks one of the Jellyfin extras kinds (`FEATURETTES`, `DELETED_SCENES`, `INTERVIEWS`, …) or returns `MAIN_MEDIA` to fall through to the normal skip. Cached per `(parent_dir, filename)`, so a release group's quirky naming pattern is classified once and free thereafter.

Concrete example:

```
source/Avatar (2009)/
  Avatar.2009.1080p.mkv
  Bonusy/                         ← unrecognised dir name
    making-of-pandora.mkv
    director-interview.mkv
```

After `organize --use-ai --apply`:

```
dest/Avatar (2009)/
  Avatar (2009).mkv
  featurettes/making-of-pandora.mkv  ← Haiku → FEATURETTES
  interviews/director-interview.mkv  ← Haiku → INTERVIEWS
```

**Operational notes:**
- Requires `ANTHROPIC_API_KEY`; the flag is always opt-in so tokens are never spent without explicit intent.
- Runs a preflight check at startup — if the key is invalid, the run aborts before any file work.
- API errors mid-run (bad key, quota, network) stop the run in `--no-interactive` mode. In interactive (sequential) mode you're prompted to disable AI and continue.
- Each call is a single system instruction + two raw strings; token usage stays minimal even on multi-thousand-file libraries.

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
  .aside/
    Futurama.S12.1080p.x265-ELiTE/
      Sample.mkv
      release.nfo
```

**Smart routing of non-canonical content (extras, samples, sidecars):**

* **Jellyfin-recognised extras** — files inside `Featurettes/`, `Behind the Scenes/`, `Deleted Scenes/`, `Interviews/`, `Trailers/`, `Shorts/`, `Bloopers/`, `DVD Extras/`, `Bonus Features/`, `Specials/` or generic `Extras/` are routed into the matching subdirectory of the parent media item (`<Movie> (Year)/featurettes/...`, `<Show>/behind the scenes/...`). Jellyfin then displays them as bonus content alongside the main title.
* **Anime OP/ED** — non-credit opening/ending tracks (`NCOP`, `NCED`, `Creditless_OP1`, files in `OP/`/`ED/`/`Openings/`/`Endings/` folders) preserve to `<Show>/extras/op-ed/`.
* **AI fallback for unknown dir names** (`--use-ai`) — pattern-based classification can't enumerate every dir variant (`Bonusy/`, `Doplnki/`, `Bonus-Materials/`, ad-hoc names). When patterns miss AND TMDB can't match as main media, Haiku reads parent-dir + filename together and picks a kind. See the dedicated [Claude Haiku AI fallbacks](#claude-haiku-ai-fallbacks---use-ai) section below for details.
* **DISCARD content** — samples, hash-named files, RARBG promo videos, and `.nfo`/`.txt`/`.jpg`/etc. sidecars go to `dest/.aside/` by default (recoverable). Pass `--remove-discards --i-mean-it` to **PERMANENTLY DELETE** them instead. Without `--i-mean-it` the run aborts with a big red warning.
* **Orphan extras** (extras folders with no parent movie/show match) fall back to `dest/.aside/`.

The `.aside/` prefix is dot-prefixed so Jellyfin ignores it during scans.

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

# Force sequential TMDB lookups (default is 40 parallel workers, throttled to 40 RPS)
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
- **DISCARD content goes to `.aside/` by default.** Sample/trailer/sidecar/`NCOP` files quarantine to `dest/.aside/` (recoverable) unless you explicitly opt in to deletion.
- **Deletion requires explicit double-flag opt-in.** Three paths can `unlink()` user files, all gated:
  - `--remove-duplicates --i-mean-it` (on `dedupe`) — deletes lower-quality duplicate copies
  - `--remove-discards --i-mean-it` (on `organize`) — deletes DISCARD-classified files (samples, NCOP/NCED, sidecars, hash-named files)
  - The interactive duplicate prompt's one-shot **`d`** option — you must type it per group; deliberately not sticky.
  Each path either prompts or shows a big red warning and aborts unless `--i-mean-it` is set.

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
