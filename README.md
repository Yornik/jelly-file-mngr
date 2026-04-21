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

### Subtitle sidecars
After each video move, subtitle files sharing the same stem (`.srt`, `.ass`, `.vtt`, `.sub`, `.ssa`, `.sup`) are moved alongside and renamed to match the destination. Language codes are preserved: `episode.en.srt` → `S01E05.en.srt`.

### Claude Haiku AI search fallback
When all title-variant retries fail to find a TMDB match, the tool can send the raw release directory and filename to `claude-haiku-4-5` for a clean search query. Set `ANTHROPIC_API_KEY` to enable — silently skipped otherwise. The prompt is a single system instruction + the two raw strings, keeping token usage minimal across large libraries.

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
export ANTHROPIC_API_KEY=your_key_here  # optional — enables AI search fallback

# Dry run — shows what would happen, nothing is moved (default)
uv run jellyfiler /path/to/messy/movies /path/to/output

# Force all files to be treated as series episodes
uv run jellyfiler /source /dest --type episode

# Apply — actually move files (still interactive by default)
uv run jellyfiler /source /dest --apply

# Non-interactive — skip ambiguous matches instead of prompting (good for automation)
uv run jellyfiler /source /dest --no-interactive --apply
```

### In-place mode

Reorganize within the source directory itself — no separate destination needed.
Useful when `movies/` and `series/` are already separate and you just want clean structure inside each.

```bash
# Dry run in-place
uv run jellyfiler /media/movies --in-place

# Apply in-place
uv run jellyfiler /media/movies --in-place --apply

# Apply in-place and remove leftover empty release folders
uv run jellyfiler /media/movies --in-place --apply --cleanup-empty-dirs
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

---

## Safety guarantees

- **Dry-run is the default.** You must pass `--apply` to move anything.
- **Nothing is ever deleted.** Files are moved, never removed. Junk is quarantined to `.junk/`, not discarded.
- **Nothing is ever overwritten.** If the destination already exists, the move is skipped.
- **Pre-flight checks run before the first file is touched.** If any problem is found (missing source, duplicate destination) the entire operation aborts with a clear error message — no partial moves.
- **Ambiguous matches are interactively resolved or skipped.** A wrong TMDB match is more dangerous than a skip. The tool defaults to asking you rather than guessing wrong.

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
