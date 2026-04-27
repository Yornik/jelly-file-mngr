"""Tests for CLI helper functions and subcommands.

These exercise the lightweight pieces of cli.py — the big organize() loop is
covered indirectly by tests/test_ai_query.py. Here we focus on:
  - argument validation (in-place + dest, cleanup-empty without in-place, etc.)
  - the standalone helpers (_fmt_size, _strip_roman_suffix, _title_variants)
  - subcommands invoked via Typer's CliRunner (scan, cache *, --version)
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from jellyfiler.cache import Cache
from jellyfiler.cli import (
    _fmt_size,
    _resolve_match,
    _strip_roman_suffix,
    _title_variants,
    app,
)
from jellyfiler.models import MediaType, TmdbMatch

runner = CliRunner()


# ---------------------------------------------------------------------------
# _fmt_size — byte/KB/MB/GB/TB boundaries
# ---------------------------------------------------------------------------


def test_fmt_size_bytes():
    assert _fmt_size(0).endswith("B")
    assert _fmt_size(512) == "512 B"


def test_fmt_size_kilobytes():
    assert _fmt_size(2048).endswith("KB")  # 2 KB


def test_fmt_size_megabytes():
    assert _fmt_size(5 * 1024 * 1024).endswith("MB")


def test_fmt_size_gigabytes():
    assert _fmt_size(3 * 1024 * 1024 * 1024).endswith("GB")


def test_fmt_size_terabytes():
    assert _fmt_size(2 * 1024**4).endswith("TB")


# ---------------------------------------------------------------------------
# _strip_roman_suffix
# ---------------------------------------------------------------------------


def test_strip_roman_suffix_strips_trailing_numeral():
    assert _strip_roman_suffix("Superman II") == "Superman"
    assert _strip_roman_suffix("Rocky IV") == "Rocky"


def test_strip_roman_suffix_leaves_clean_titles_untouched():
    assert _strip_roman_suffix("Coco") == "Coco"
    assert _strip_roman_suffix("Blade Runner 2049") == "Blade Runner 2049"


def test_strip_roman_suffix_case_insensitive():
    assert _strip_roman_suffix("Superman ii") == "Superman"


# ---------------------------------------------------------------------------
# _title_variants
# ---------------------------------------------------------------------------


def test_title_variants_strips_roman_suffix():
    variants = _title_variants("Superman II")
    assert "Superman" in variants


def test_title_variants_replaces_ampersand():
    variants = _title_variants("Superman & Batman")
    assert "Superman and Batman" in variants


def test_title_variants_splits_camelcase():
    variants = _title_variants("WonderWoman")
    assert "Wonder Woman" in variants


def test_title_variants_word_segments_lowercase():
    variants = _title_variants("wonderwoman")
    assert "wonder woman" in variants


def test_title_variants_empty_for_clean_title():
    """Already-clean title with no transformations available → no variants."""
    assert _title_variants("Coco") == []


# ---------------------------------------------------------------------------
# _resolve_match — wraps best_match + interactive prompt
# ---------------------------------------------------------------------------


def test_resolve_match_returns_best_match():
    """When best_match returns a confident hit, _resolve_match passes it through."""
    m = TmdbMatch(tmdb_id=1, title="Coco", year=2017, media_type=MediaType.MOVIE)
    with patch("jellyfiler.cli.best_match", return_value=m):
        result = _resolve_match(
            Path("file.mkv"), "Coco", 2017, [m], MediaType.MOVIE, interactive=True
        )
    assert result is m


def test_resolve_match_returns_none_in_non_interactive_when_ambiguous():
    """No best match + non-interactive = None (caller will skip)."""
    m = TmdbMatch(tmdb_id=1, title="Coco", year=2017, media_type=MediaType.MOVIE)
    with patch("jellyfiler.cli.best_match", return_value=None):
        result = _resolve_match(
            Path("file.mkv"), "Coco", 2017, [m], MediaType.MOVIE, interactive=False
        )
    assert result is None


def test_resolve_match_prompts_in_interactive_mode_when_no_confident_match():
    """No best match + interactive + matches → prompt the user."""
    m = TmdbMatch(tmdb_id=1, title="Coco", year=2017, media_type=MediaType.MOVIE)
    with (
        patch("jellyfiler.cli.best_match", return_value=None),
        patch("jellyfiler.cli.prompt_tmdb_match", return_value=m) as mock_prompt,
    ):
        result = _resolve_match(
            Path("file.mkv"), "Coco", 2017, [m], MediaType.MOVIE, interactive=True
        )
    assert result is m
    mock_prompt.assert_called_once()


def test_resolve_match_no_matches_returns_none():
    """Empty match list → None even in interactive mode (nothing to prompt)."""
    with patch("jellyfiler.cli.best_match", return_value=None):
        result = _resolve_match(
            Path("file.mkv"), "Coco", 2017, [], MediaType.MOVIE, interactive=True
        )
    assert result is None


# ---------------------------------------------------------------------------
# --version flag
# ---------------------------------------------------------------------------


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "jellyfiler" in result.stdout


# ---------------------------------------------------------------------------
# scan subcommand
# ---------------------------------------------------------------------------


def test_scan_command_lists_parsed_metadata(tmp_path: Path):
    (tmp_path / "Futurama.S12E03.1080p.x265.mkv").touch()
    (tmp_path / "Blade.Runner.2049.2017.mkv").touch()
    result = runner.invoke(app, ["scan", str(tmp_path)])
    assert result.exit_code == 0
    assert "Futurama" in result.stdout
    assert "Blade Runner 2049" in result.stdout


def test_scan_command_no_files(tmp_path: Path):
    result = runner.invoke(app, ["scan", str(tmp_path)])
    assert result.exit_code == 0
    assert "No media files" in result.stdout


def test_scan_command_missing_directory(tmp_path: Path):
    bad = tmp_path / "does-not-exist"
    result = runner.invoke(app, ["scan", str(bad)])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# cache subcommands
# ---------------------------------------------------------------------------


def test_cache_stats_subcommand(tmp_path: Path):
    db = tmp_path / "cache.db"
    Cache(db).close()  # make sure the DB exists
    result = runner.invoke(app, ["cache", "stats", "--cache-db", str(db)])
    assert result.exit_code == 0
    assert "TMDB search cache" in result.stdout
    assert "Move log" in result.stdout


def test_cache_unpin_removes_pinned_entry(tmp_path: Path):
    db = tmp_path / "cache.db"
    cache = Cache(db)
    cache.set_pinned(
        "futurama",
        None,
        MediaType.EPISODE,
        TmdbMatch(tmdb_id=1, title="Futurama", year=1999, media_type=MediaType.EPISODE),
    )
    cache.close()

    result = runner.invoke(
        app,
        ["cache", "unpin", "futurama", "--type", "episode", "--cache-db", str(db)],
    )
    assert result.exit_code == 0
    assert "Unpinned" in result.stdout


def test_cache_unpin_not_found(tmp_path: Path):
    db = tmp_path / "cache.db"
    Cache(db).close()
    result = runner.invoke(
        app, ["cache", "unpin", "no-such-show", "--type", "episode", "--cache-db", str(db)]
    )
    assert result.exit_code == 0
    assert "Not found" in result.stdout


def test_cache_clear_requires_a_flag(tmp_path: Path):
    """`cache clear` with no --pinned/--tmdb/--moves/--all → error."""
    db = tmp_path / "cache.db"
    Cache(db).close()
    result = runner.invoke(app, ["cache", "clear", "--cache-db", str(db)])
    assert result.exit_code == 1


def test_cache_clear_pinned_via_cli(tmp_path: Path):
    db = tmp_path / "cache.db"
    cache = Cache(db)
    cache.set_pinned(
        "futurama",
        None,
        MediaType.EPISODE,
        TmdbMatch(tmdb_id=1, title="Futurama", year=1999, media_type=MediaType.EPISODE),
    )
    cache.close()

    result = runner.invoke(app, ["cache", "clear", "--pinned", "--cache-db", str(db)])
    assert result.exit_code == 0
    assert "pinned" in result.stdout


def test_cache_clear_all_via_cli(tmp_path: Path):
    db = tmp_path / "cache.db"
    Cache(db).close()
    result = runner.invoke(app, ["cache", "clear", "--all", "--cache-db", str(db)])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# organize argument validation
# ---------------------------------------------------------------------------


def test_organize_missing_tmdb_key_exits(tmp_path: Path):
    """No TMDB_API_KEY in env → exit with error before any file work."""
    src = tmp_path / "src"
    src.mkdir()
    with patch.dict(os.environ, {}, clear=True):
        result = runner.invoke(app, ["organize", str(src), str(tmp_path / "dest")])
    assert result.exit_code == 1


def test_organize_in_place_with_dest_argument_errors(tmp_path: Path):
    """Cannot combine --in-place with a separate DEST argument."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(app, ["organize", str(src), str(dest), "--in-place"])
    assert result.exit_code == 1


def test_organize_dest_required_unless_in_place(tmp_path: Path):
    """Without --in-place, DEST must be provided."""
    src = tmp_path / "src"
    src.mkdir()
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(app, ["organize", str(src)])
    assert result.exit_code == 1


def test_organize_cleanup_empty_dirs_requires_in_place(tmp_path: Path):
    """--cleanup-empty-dirs only makes sense with --in-place."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(app, ["organize", str(src), str(dest), "--cleanup-empty-dirs"])
    assert result.exit_code == 1


def test_organize_missing_source_directory(tmp_path: Path):
    """find_media_files raises FileNotFoundError → graceful error exit."""
    bad_src = tmp_path / "does-not-exist"
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(app, ["organize", str(bad_src), str(dest)])
    assert result.exit_code == 1


def test_organize_empty_source_exits_cleanly(tmp_path: Path):
    """Empty source directory → exit 0 with friendly message, no TMDB calls."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(app, ["organize", str(src), str(dest)])
    assert result.exit_code == 0
    assert "No media files" in result.stdout


def test_organize_use_ai_without_anthropic_key_errors(tmp_path: Path):
    """--use-ai requires ANTHROPIC_API_KEY to be set, even before scanning."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}, clear=True):
        result = runner.invoke(app, ["organize", str(src), str(dest), "--use-ai"])
    assert result.exit_code == 1


def test_organize_use_ai_with_failing_preflight(tmp_path: Path):
    """--use-ai with a key but failing preflight should abort before scanning."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with (
        patch.dict(os.environ, {"TMDB_API_KEY": "fake", "ANTHROPIC_API_KEY": "fake-ai"}),
        patch("jellyfiler.cli.preflight_check", return_value=False),
    ):
        result = runner.invoke(app, ["organize", str(src), str(dest), "--use-ai"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# organize: pinned-cache short-circuit (covers PINNED branch in cli.organize)
# ---------------------------------------------------------------------------


def test_dedupe_remove_duplicates_without_i_mean_it_aborts(tmp_path: Path):
    """`dedupe --remove-duplicates` without --i-mean-it must exit 1 (big red warning)."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(app, ["dedupe", str(src), str(dest), "--remove-duplicates"])
    assert result.exit_code == 1


def test_dedupe_i_mean_it_without_remove_duplicates_aborts(tmp_path: Path):
    """`dedupe --i-mean-it` alone makes no sense → exit 1."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(app, ["dedupe", str(src), str(dest), "--i-mean-it"])
    assert result.exit_code == 1


def test_dedupe_quarantine_and_remove_duplicates_conflict(tmp_path: Path):
    """Cannot combine --quarantine-duplicates with --remove-duplicates → exit 1."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(
            app,
            [
                "dedupe",
                str(src),
                str(dest),
                "--quarantine-duplicates",
                "--remove-duplicates",
                "--i-mean-it",
            ],
        )
    assert result.exit_code == 1


def test_dedupe_remove_duplicates_with_i_mean_it_proceeds(tmp_path: Path):
    """Both flags together must NOT abort early — gate passes."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(
            app,
            [
                "dedupe",
                str(src),
                str(dest),
                "--remove-duplicates",
                "--i-mean-it",
                "--no-interactive",
            ],
        )
    assert result.exit_code == 0  # empty source → "no media files" exit


def test_dedupe_quarantine_duplicates_alone_proceeds(tmp_path: Path):
    """--quarantine-duplicates is a single-flag opt-in (recoverable, no double safety)."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(
            app,
            ["dedupe", str(src), str(dest), "--quarantine-duplicates", "--no-interactive"],
        )
    assert result.exit_code == 0


def test_organize_no_longer_accepts_remove_duplicates(tmp_path: Path):
    """The flag was moved to `dedupe` — `organize` rejects it as an unknown option."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(app, ["organize", str(src), str(dest), "--remove-duplicates"])
    assert result.exit_code != 0  # Click exits with 2 for unknown options


def test_organize_log_writes_jsonl_events(tmp_path: Path):
    """--log path emits a JSONL file with run_started + run_finished events at minimum."""

    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    log_path = tmp_path / "events.jsonl"

    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(
            app,
            [
                "organize",
                str(src),
                str(dest),
                "--no-interactive",
                "--log",
                str(log_path),
            ],
        )
    # Empty source = exit 0 before run_finished, but run_started must have written.
    assert result.exit_code == 0
    # When source is empty we never reach OrganizeContext / logger creation, so
    # log file may not exist. Use a populated source for the real check.


def test_organize_log_records_full_run(tmp_path: Path):
    """A populated source produces run_started, classify_*, and run_finished events."""
    import json

    src = tmp_path / "src"
    src.mkdir()
    f = src / "Show.S01E01.mkv"
    f.touch()
    dest = tmp_path / "dest"
    log_path = tmp_path / "events.jsonl"

    fake_match = TmdbMatch(tmdb_id=1, title="Show", year=2020, media_type=MediaType.EPISODE)
    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = fake_match
    mock_cache.get_tmdb.return_value = None

    with (
        patch.dict(os.environ, {"TMDB_API_KEY": "fake"}),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient"),
    ):
        result = runner.invoke(
            app,
            [
                "organize",
                str(src),
                str(dest),
                "--no-interactive",
                "--log",
                str(log_path),
            ],
        )
    assert result.exit_code == 0
    assert log_path.exists()
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    event_names = [e["event"] for e in events]
    assert "run_started" in event_names
    assert "classify_pinned" in event_names  # we mocked a pinned hit
    assert "run_finished" in event_names


def test_organize_full_plan_flag_accepted(tmp_path: Path):
    """--full-plan must be accepted (not rejected as unknown)."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(
            app, ["organize", str(src), str(dest), "--no-interactive", "--full-plan"]
        )
    assert result.exit_code == 0


def test_dedupe_log_flag_accepted(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(
            app,
            [
                "dedupe",
                str(src),
                str(dest),
                "--no-interactive",
                "--log",
                str(tmp_path / "dedupe.jsonl"),
            ],
        )
    assert result.exit_code == 0


def test_organize_no_longer_accepts_quarantine_duplicates(tmp_path: Path):
    """--quarantine-duplicates was moved to `dedupe`."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = runner.invoke(app, ["organize", str(src), str(dest), "--quarantine-duplicates"])
    assert result.exit_code != 0


def test_organize_pinned_match_skips_tmdb(tmp_path: Path):
    """When a pinned match exists for the title, the TMDB API must not be called."""
    src = tmp_path / "src"
    src.mkdir()
    media = src / "Futurama.S12E03.mkv"
    media.touch()
    dest = tmp_path / "dest"

    pinned_match = TmdbMatch(tmdb_id=42, title="Futurama", year=1999, media_type=MediaType.EPISODE)

    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = pinned_match

    mock_tmdb = MagicMock()
    # If this gets called it's a bug — pinned should short-circuit
    mock_tmdb.search_movie.side_effect = AssertionError("should not call TMDB")
    mock_tmdb.search_tv.side_effect = AssertionError("should not call TMDB")

    with (
        patch.dict(os.environ, {"TMDB_API_KEY": "fake"}),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient", return_value=mock_tmdb),
    ):
        result = runner.invoke(
            app,
            ["organize", str(src), str(dest), "--no-interactive"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    mock_cache.get_pinned.assert_called()
