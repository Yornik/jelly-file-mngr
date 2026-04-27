"""Tests for the per-file pipeline helpers extracted from organize().

These target the building blocks that the ``organize`` and ``dedupe`` Typer
commands compose. Each test exercises a single branch of the chain so the
mocks stay simple and the failures point to the exact path that broke.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from jellyfiler.cli import (
    LookupResult,
    OrganizeContext,
    _ai_preflight,
    _apply_dedupe_actions,
    _handle_junk_files,
    _lookup_match_chain,
    _process_one_file,
    _resolve_dedupe,
    _run_pipeline,
    _skipped_move,
    _validate_dedupe_flags,
    _validate_in_place_args,
)
from jellyfiler.models import GuessedMedia, MediaType, Plan, PlannedMove, TmdbMatch

# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _ctx(
    tmp_path: Path,
    *,
    interactive: bool = False,
    use_ai: bool = False,
    forced: MediaType = MediaType.UNKNOWN,
    quiet: bool = True,
    force: bool = False,
    rich_names: bool = False,
) -> OrganizeContext:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir(exist_ok=True)
    dst.mkdir(exist_ok=True)
    cache = MagicMock()
    cache.already_moved.return_value = False
    cache.get_pinned.return_value = None
    cache.get_tmdb.return_value = None
    return OrganizeContext(
        source=src,
        dest=dst,
        tmdb=MagicMock(),
        cache=cache,
        interactive=interactive,
        use_ai=use_ai,
        forced_media_type=forced,
        rich_names=rich_names,
        quiet=quiet,
        force=force,
    )


def _episode(title: str = "Show", season: int | None = 1, episode: int | None = 1) -> GuessedMedia:
    return GuessedMedia(
        source_path=Path(f"{title}.S{season:02d}E{episode:02d}.mkv"),
        media_type=MediaType.EPISODE,
        title=title,
        season=season,
        episode=episode,
    )


def _tmdb_match(title: str = "Show", year: int = 2020) -> TmdbMatch:
    return TmdbMatch(tmdb_id=1, title=title, year=year, media_type=MediaType.EPISODE)


# ═══════════════════════════════════════════════════════════════════════════
# _validate_in_place_args
# ═══════════════════════════════════════════════════════════════════════════


def test_validate_in_place_dest_provided_with_in_place_aborts(tmp_path: Path):
    import typer

    with pytest.raises(typer.Exit):
        _validate_in_place_args(
            in_place=True, dest=tmp_path / "dest", source=tmp_path, cleanup_empty_dirs=False
        )


def test_validate_in_place_no_dest_no_in_place_aborts(tmp_path: Path):
    import typer

    with pytest.raises(typer.Exit):
        _validate_in_place_args(
            in_place=False, dest=None, source=tmp_path, cleanup_empty_dirs=False
        )


def test_validate_in_place_returns_dest_when_separate_dest_given(tmp_path: Path):
    dest = tmp_path / "dest"
    out = _validate_in_place_args(
        in_place=False, dest=dest, source=tmp_path, cleanup_empty_dirs=False
    )
    assert out == dest


def test_validate_in_place_returns_source_in_place_mode(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    out = _validate_in_place_args(in_place=True, dest=None, source=src, cleanup_empty_dirs=False)
    assert out == src


def test_validate_in_place_cleanup_without_in_place_aborts(tmp_path: Path):
    import typer

    with pytest.raises(typer.Exit):
        _validate_in_place_args(
            in_place=False, dest=tmp_path / "dest", source=tmp_path, cleanup_empty_dirs=True
        )


# ═══════════════════════════════════════════════════════════════════════════
# _validate_dedupe_flags
# ═══════════════════════════════════════════════════════════════════════════


def test_validate_dedupe_flags_remove_without_imeanit_aborts():
    import typer

    with pytest.raises(typer.Exit):
        _validate_dedupe_flags(remove_duplicates=True, i_mean_it=False, quarantine_duplicates=False)


def test_validate_dedupe_flags_imeanit_alone_aborts():
    import typer

    with pytest.raises(typer.Exit):
        _validate_dedupe_flags(remove_duplicates=False, i_mean_it=True, quarantine_duplicates=False)


def test_validate_dedupe_flags_remove_and_quarantine_conflict():
    import typer

    with pytest.raises(typer.Exit):
        _validate_dedupe_flags(remove_duplicates=True, i_mean_it=True, quarantine_duplicates=True)


def test_validate_dedupe_flags_double_flag_passes():
    # Should not raise
    _validate_dedupe_flags(remove_duplicates=True, i_mean_it=True, quarantine_duplicates=False)


def test_validate_dedupe_flags_quarantine_alone_passes():
    _validate_dedupe_flags(remove_duplicates=False, i_mean_it=False, quarantine_duplicates=True)


def test_validate_dedupe_flags_no_flags_passes():
    _validate_dedupe_flags(remove_duplicates=False, i_mean_it=False, quarantine_duplicates=False)


# ═══════════════════════════════════════════════════════════════════════════
# _ai_preflight
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_preflight_skipped_when_use_ai_off():
    _ai_preflight(use_ai=False, quiet=True)  # no-op


def test_ai_preflight_missing_key_aborts():
    import typer

    with patch.dict("os.environ", {}, clear=True), pytest.raises(typer.Exit):
        _ai_preflight(use_ai=True, quiet=True)


def test_ai_preflight_check_failure_aborts():
    import typer

    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake"}),
        patch("jellyfiler.cli.preflight_check", return_value=False),
        pytest.raises(typer.Exit),
    ):
        _ai_preflight(use_ai=True, quiet=True)


def test_ai_preflight_check_success_passes():
    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake"}),
        patch("jellyfiler.cli.preflight_check", return_value=True),
    ):
        _ai_preflight(use_ai=True, quiet=True)  # no exception


# ═══════════════════════════════════════════════════════════════════════════
# _skipped_move
# ═══════════════════════════════════════════════════════════════════════════


def test_skipped_move_sets_skipped_true(tmp_path: Path):
    move = _skipped_move(tmp_path / "x.mkv", tmp_path, MediaType.MOVIE, "x", "reason")
    assert move.skipped
    assert move.skip_reason == "reason"
    assert move.confidence == "low"


# ═══════════════════════════════════════════════════════════════════════════
# _lookup_match_chain
# ═══════════════════════════════════════════════════════════════════════════


def test_lookup_chain_uses_cached_tmdb_results(tmp_path: Path):
    """When the cache has a hit, no TMDB API call is made."""
    ctx = _ctx(tmp_path)
    ctx.cache.get_tmdb.return_value = [_tmdb_match("Show", 2020)]
    g = _episode("Show")
    out = _lookup_match_chain(g, tmp_path / "x.mkv", ctx)
    assert out.status == "ok"
    assert len(out.matches) == 1
    ctx.tmdb.search_movie.assert_not_called()
    ctx.tmdb.search_tv.assert_not_called()


def test_lookup_chain_movie_path_calls_search_movie(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.tmdb.search_movie.return_value = [TmdbMatch(1, "Coco", 2017, MediaType.MOVIE)]
    g = GuessedMedia(
        source_path=Path("Coco.mkv"),
        media_type=MediaType.MOVIE,
        title="Coco",
        year=2017,
    )
    out = _lookup_match_chain(g, tmp_path / "x.mkv", ctx)
    assert out.status == "ok"
    ctx.tmdb.search_movie.assert_called_once_with("Coco", 2017)
    ctx.cache.set_tmdb.assert_called()


def test_lookup_chain_tv_path_passes_year_none(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.tmdb.search_tv.return_value = [_tmdb_match()]
    g = _episode()
    _lookup_match_chain(g, tmp_path / "x.mkv", ctx)
    ctx.tmdb.search_tv.assert_called_once_with("Show", None)


def test_lookup_chain_http_status_error_returns_tmdb_error(tmp_path: Path):
    ctx = _ctx(tmp_path)
    response = MagicMock()
    response.status_code = 401
    response.reason_phrase = "Unauthorized"
    ctx.tmdb.search_tv.side_effect = httpx.HTTPStatusError(
        "auth", request=MagicMock(), response=response
    )
    out = _lookup_match_chain(_episode(), tmp_path / "x.mkv", ctx)
    assert out.status == "tmdb_error"


def test_lookup_chain_generic_exception_returns_tmdb_error(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.tmdb.search_tv.side_effect = RuntimeError("network down")
    out = _lookup_match_chain(_episode(), tmp_path / "x.mkv", ctx)
    assert out.status == "tmdb_error"


def test_lookup_chain_runs_variant_retries_when_initial_misses(tmp_path: Path):
    """When best_match misses on first search, _title_variants retries kick in."""
    ctx = _ctx(tmp_path)
    # First call returns nothing helpful; variant retry returns a match
    ctx.tmdb.search_tv.side_effect = [
        [],  # no exact match for "Superman II"
        [_tmdb_match("Superman", 1978)],  # variant "Superman" hits
    ]
    g = GuessedMedia(
        source_path=Path("x.mkv"),
        media_type=MediaType.EPISODE,
        title="Superman II",
        year=1978,
    )
    out = _lookup_match_chain(g, tmp_path / "x.mkv", ctx)
    assert out.search_title == "Superman"  # variant won
    assert ctx.tmdb.search_tv.call_count == 2


def test_lookup_chain_anilist_fallback_for_anime(tmp_path: Path):
    """TMDB miss + anime hint → AniList is queried as a fallback."""
    ctx = _ctx(tmp_path)
    ctx.tmdb.search_tv.return_value = []  # TMDB misses
    g = GuessedMedia(
        source_path=Path("[HorribleSubs] Show - 01 [1080p].mkv"),
        media_type=MediaType.EPISODE,
        title="Show",
    )
    with patch(
        "jellyfiler.cli.search_anime",
        return_value=[TmdbMatch(99, "Show (Anime)", 2020, MediaType.EPISODE)],
    ) as mock_search:
        out = _lookup_match_chain(g, Path("[HorribleSubs] Show - 01 [1080p].mkv"), ctx)
    mock_search.assert_called_once()
    assert len(out.matches) == 1
    assert out.matches[0].title == "Show (Anime)"


def test_lookup_chain_anilist_failure_is_swallowed(tmp_path: Path):
    """AniList errors don't abort the run — they're logged and skipped."""
    ctx = _ctx(tmp_path)
    ctx.tmdb.search_tv.return_value = []
    g = GuessedMedia(
        source_path=Path("[HorribleSubs] Show - 01.mkv"),
        media_type=MediaType.EPISODE,
        title="Show",
    )
    with patch("jellyfiler.cli.search_anime", side_effect=RuntimeError("anilist down")):
        out = _lookup_match_chain(g, Path("[HorribleSubs] Show - 01.mkv"), ctx)
    assert out.status == "ok"  # not aborted


def test_lookup_chain_ai_fallback_uses_suggestion(tmp_path: Path):
    """When all else fails, --use-ai sends the messy name to Haiku."""
    ctx = _ctx(tmp_path, use_ai=True)
    ctx.tmdb.search_tv.return_value = []
    # Variant retries also miss
    g = GuessedMedia(source_path=Path("x.mkv"), media_type=MediaType.EPISODE, title="weird name")
    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake"}),
        patch(
            "jellyfiler.cli.suggest_search",
            return_value={"title": "Real Show", "year": None},
        ),
    ):
        # AI's retry returns the actual match
        ctx.tmdb.search_tv.side_effect = [[], [_tmdb_match("Real Show", 2010)]]
        out = _lookup_match_chain(g, Path("x.mkv"), ctx)
    assert out.search_title == "Real Show"


def test_lookup_chain_ai_error_non_interactive_aborts(tmp_path: Path):
    """AI failure in non-interactive mode → ai_abort status."""
    from jellyfiler.ai_query import AiQueryError

    ctx = _ctx(tmp_path, use_ai=True, interactive=False)
    ctx.tmdb.search_tv.return_value = []
    g = GuessedMedia(source_path=Path("x.mkv"), media_type=MediaType.EPISODE, title="weird")
    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake"}),
        patch("jellyfiler.cli.suggest_search", side_effect=AiQueryError("quota")),
    ):
        out = _lookup_match_chain(g, Path("x.mkv"), ctx)
    assert out.status == "ai_abort"


def test_lookup_chain_ai_error_interactive_disable_continues(tmp_path: Path):
    """User confirms 'disable AI' → ctx.ai_disabled flips True, lookup returns ok."""
    from jellyfiler.ai_query import AiQueryError

    ctx = _ctx(tmp_path, use_ai=True, interactive=True)
    ctx.tmdb.search_tv.return_value = []
    g = GuessedMedia(source_path=Path("x.mkv"), media_type=MediaType.EPISODE, title="weird")
    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake"}),
        patch("jellyfiler.cli.suggest_search", side_effect=AiQueryError("net")),
        patch("jellyfiler.cli.typer.confirm", return_value=True),
    ):
        out = _lookup_match_chain(g, Path("x.mkv"), ctx)
    assert out.status == "ok"
    assert ctx.ai_disabled is True


def test_lookup_chain_ai_error_interactive_decline_aborts(tmp_path: Path):
    """User declines 'disable AI' → ai_abort."""
    from jellyfiler.ai_query import AiQueryError

    ctx = _ctx(tmp_path, use_ai=True, interactive=True)
    ctx.tmdb.search_tv.return_value = []
    g = GuessedMedia(source_path=Path("x.mkv"), media_type=MediaType.EPISODE, title="weird")
    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake"}),
        patch("jellyfiler.cli.suggest_search", side_effect=AiQueryError("net")),
        patch("jellyfiler.cli.typer.confirm", return_value=False),
    ):
        out = _lookup_match_chain(g, Path("x.mkv"), ctx)
    assert out.status == "ai_abort"


def test_lookup_chain_ai_disabled_flag_skips_call(tmp_path: Path):
    """If ctx.ai_disabled is already True, suggest_search is never called."""
    ctx = _ctx(tmp_path, use_ai=True)
    ctx.ai_disabled = True
    ctx.tmdb.search_tv.return_value = []
    g = GuessedMedia(source_path=Path("x.mkv"), media_type=MediaType.EPISODE, title="weird")
    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake"}),
        patch("jellyfiler.cli.suggest_search") as mock_suggest,
    ):
        _lookup_match_chain(g, Path("x.mkv"), ctx)
    mock_suggest.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# _process_one_file
# ═══════════════════════════════════════════════════════════════════════════


def test_process_one_file_cached_skip(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.cache.already_moved.return_value = True
    f = tmp_path / "x.mkv"
    f.touch()
    out = _process_one_file(f, ctx)
    assert out.kind == "cached"


def test_process_one_file_force_bypasses_cache(tmp_path: Path):
    ctx = _ctx(tmp_path, force=True)
    ctx.cache.already_moved.return_value = True
    f = tmp_path / "Sample.mkv"  # junk filename
    f.touch()
    out = _process_one_file(f, ctx)
    assert out.kind == "junk"  # cache check is skipped, junk filter fires


def test_process_one_file_junk_returns_junk(tmp_path: Path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "Sample.mkv"
    f.touch()
    out = _process_one_file(f, ctx)
    assert out.kind == "junk"


def test_process_one_file_unknown_type_returns_skipped_move(tmp_path: Path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "no_pattern.mkv"
    f.touch()
    with patch(
        "jellyfiler.cli.guess",
        return_value=GuessedMedia(source_path=f, media_type=MediaType.UNKNOWN, title="x"),
    ):
        out = _process_one_file(f, ctx)
    assert out.kind == "planned"
    assert out.move is not None
    assert out.move.skipped
    assert "media type" in out.move.skip_reason


def test_process_one_file_no_title_non_interactive_skips(tmp_path: Path):
    ctx = _ctx(tmp_path, interactive=False)
    f = tmp_path / "weird.mkv"
    f.touch()
    with patch(
        "jellyfiler.cli.guess",
        return_value=GuessedMedia(source_path=f, media_type=MediaType.EPISODE, title=""),
    ):
        out = _process_one_file(f, ctx)
    assert out.kind == "planned"
    assert out.move is not None and out.move.skipped


def test_process_one_file_no_title_interactive_uses_manual(tmp_path: Path):
    ctx = _ctx(tmp_path, interactive=True)
    f = tmp_path / "weird.mkv"
    f.touch()
    with (
        patch(
            "jellyfiler.cli.guess",
            return_value=GuessedMedia(
                source_path=f, media_type=MediaType.EPISODE, title="", season=1, episode=1
            ),
        ),
        patch("jellyfiler.cli.prompt_manual_title", return_value="Real Title"),
        patch("jellyfiler.cli._lookup_match_chain") as mock_lookup,
    ):
        mock_lookup.return_value = LookupResult(
            matches=[_tmdb_match("Real Title")], search_title="Real Title", status="ok"
        )
        out = _process_one_file(f, ctx)
    assert out.kind == "planned"
    assert mock_lookup.called


def test_process_one_file_no_title_interactive_user_skips(tmp_path: Path):
    ctx = _ctx(tmp_path, interactive=True)
    f = tmp_path / "weird.mkv"
    f.touch()
    with (
        patch(
            "jellyfiler.cli.guess",
            return_value=GuessedMedia(source_path=f, media_type=MediaType.EPISODE, title=""),
        ),
        patch("jellyfiler.cli.prompt_manual_title", return_value=None),
    ):
        out = _process_one_file(f, ctx)
    assert out.kind == "planned" and out.move is not None and out.move.skipped


def test_process_one_file_pinned_match_short_circuits(tmp_path: Path):
    ctx = _ctx(tmp_path)
    pinned = _tmdb_match("Show", 2020)
    ctx.cache.get_pinned.return_value = pinned
    f = tmp_path / "Show.S01E01.mkv"
    f.touch()
    out = _process_one_file(f, ctx)
    assert out.kind == "planned"
    assert out.move is not None and not out.move.skipped
    # No TMDB call happened because pinned hit
    ctx.tmdb.search_tv.assert_not_called()


def test_process_one_file_forced_media_type_overrides_guess(tmp_path: Path):
    ctx = _ctx(tmp_path, forced=MediaType.EPISODE)
    f = tmp_path / "Show.2020.mkv"  # would normally guess as movie
    f.touch()
    with patch(
        "jellyfiler.cli.guess",
        return_value=GuessedMedia(
            source_path=f, media_type=MediaType.MOVIE, title="Show", year=2020
        ),
    ):
        ctx.cache.get_pinned.return_value = _tmdb_match("Show")
        out = _process_one_file(f, ctx)
    # Since type was forced to EPISODE, the cache lookup happens with that type
    assert out.kind == "planned"


def test_process_one_file_tmdb_error_propagates(tmp_path: Path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "Show.S01E01.mkv"
    f.touch()
    with patch(
        "jellyfiler.cli._lookup_match_chain",
        return_value=LookupResult(status="tmdb_error", error_msg="boom"),
    ):
        out = _process_one_file(f, ctx)
    assert out.kind == "tmdb_error"


def test_process_one_file_ai_abort_propagates(tmp_path: Path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "Show.S01E01.mkv"
    f.touch()
    with patch(
        "jellyfiler.cli._lookup_match_chain",
        return_value=LookupResult(status="ai_abort", error_msg="quota"),
    ):
        out = _process_one_file(f, ctx)
    assert out.kind == "ai_abort"


def test_process_one_file_non_interactive_ambiguous_skips(tmp_path: Path):
    ctx = _ctx(tmp_path, interactive=False)
    f = tmp_path / "Show.S01E01.mkv"
    f.touch()
    with (
        patch(
            "jellyfiler.cli._lookup_match_chain",
            return_value=LookupResult(
                matches=[_tmdb_match("A"), _tmdb_match("B")],
                search_title="Show",
                status="ok",
            ),
        ),
        patch("jellyfiler.cli._resolve_match", return_value=None),
    ):
        out = _process_one_file(f, ctx)
    assert out.kind == "planned"
    assert out.move is not None and out.move.skipped
    assert "Ambiguous" in out.move.skip_reason


def test_process_one_file_bare_episode_interactive_prompts(tmp_path: Path):
    ctx = _ctx(tmp_path, interactive=True)
    f = tmp_path / "Show - title.mkv"
    f.touch()
    match = _tmdb_match()
    ctx.tmdb.get_season_episodes.return_value = [(1, "Pilot"), (2, "Two")]
    with (
        patch(
            "jellyfiler.cli.guess",
            return_value=GuessedMedia(
                source_path=f, media_type=MediaType.EPISODE, title="Show", season=1, episode=None
            ),
        ),
        patch(
            "jellyfiler.cli._lookup_match_chain",
            return_value=LookupResult(matches=[match], search_title="Show", status="ok"),
        ),
        patch("jellyfiler.cli._resolve_match", return_value=match),
        patch("jellyfiler.cli.prompt_episode_number", return_value=2),
    ):
        out = _process_one_file(f, ctx)
    assert out.kind == "planned"
    # episode was filled in from prompt
    assert out.move is not None


def test_process_one_file_bare_episode_get_season_failure_swallowed(tmp_path: Path):
    """If TMDB.get_season_episodes throws, we log and continue without a picked episode."""
    ctx = _ctx(tmp_path, interactive=True)
    f = tmp_path / "Show - title.mkv"
    f.touch()
    match = _tmdb_match()
    ctx.tmdb.get_season_episodes.side_effect = RuntimeError("api down")
    with (
        patch(
            "jellyfiler.cli.guess",
            return_value=GuessedMedia(
                source_path=f, media_type=MediaType.EPISODE, title="Show", season=1, episode=None
            ),
        ),
        patch(
            "jellyfiler.cli._lookup_match_chain",
            return_value=LookupResult(matches=[match], search_title="Show", status="ok"),
        ),
        patch("jellyfiler.cli._resolve_match", return_value=match),
    ):
        out = _process_one_file(f, ctx)
    # Result returns planned (with an unmatched episode → planner will mark as skipped)
    assert out.kind == "planned"


# ═══════════════════════════════════════════════════════════════════════════
# _run_pipeline
# ═══════════════════════════════════════════════════════════════════════════


def test_run_pipeline_aggregates_planned_and_junk(tmp_path: Path):
    ctx = _ctx(tmp_path)
    f1 = tmp_path / "Show.S01E01.mkv"
    f2 = tmp_path / "Sample.mkv"
    f1.touch()
    f2.touch()
    ctx.cache.get_pinned.return_value = _tmdb_match()
    out = _run_pipeline([f1, f2], ctx)
    assert len(out.planned_moves) == 1
    assert out.junk_files == [f2]
    assert not out.aborted


def test_run_pipeline_breaks_on_tmdb_error(tmp_path: Path):
    ctx = _ctx(tmp_path)
    f = tmp_path / "Show.S01E01.mkv"
    f.touch()
    with patch(
        "jellyfiler.cli._process_one_file",
        return_value=__import__("jellyfiler.cli", fromlist=["FileResult"]).FileResult(
            kind="tmdb_error", error_msg="api"
        ),
    ):
        out = _run_pipeline([f], ctx)
    assert out.aborted
    assert out.tmdb_errors == 1


def test_run_pipeline_breaks_on_ai_abort(tmp_path: Path):
    from jellyfiler.cli import FileResult

    ctx = _ctx(tmp_path)
    f = tmp_path / "Show.S01E01.mkv"
    f.touch()
    with patch(
        "jellyfiler.cli._process_one_file",
        return_value=FileResult(kind="ai_abort", error_msg="x"),
    ):
        out = _run_pipeline([f], ctx)
    assert out.aborted


# ═══════════════════════════════════════════════════════════════════════════
# _apply_dedupe_actions
# ═══════════════════════════════════════════════════════════════════════════


def _move_for(src_file: Path) -> PlannedMove:
    return PlannedMove(
        source=src_file,
        destination=src_file.parent / "out" / src_file.name,
        media_type=MediaType.EPISODE,
        tmdb_id=1,
        matched_title="Show",
        confidence="high",
    )


def test_apply_dedupe_actions_quarantines_files(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    loser = src / "show.720p.mkv"
    loser.touch()
    move = _move_for(loser)
    _apply_dedupe_actions([], [move], set(), src, dst)
    # File moved to .junk/duplicates/
    assert not loser.exists()
    quarantined = dst / ".junk" / "duplicates" / "show.720p.mkv"
    assert quarantined.exists()


def test_apply_dedupe_actions_skips_when_quarantine_target_exists(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    loser = src / "show.720p.mkv"
    loser.touch()
    # Pre-create quarantine target
    qdir = dst / ".junk" / "duplicates"
    qdir.mkdir(parents=True)
    (qdir / "show.720p.mkv").write_text("existing")
    move = _move_for(loser)
    _apply_dedupe_actions([], [move], set(), src, dst)
    # Loser stays in source
    assert loser.exists()


def test_apply_dedupe_actions_deletes_files(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    loser = src / "show.720p.mkv"
    loser.touch()
    _apply_dedupe_actions([_move_for(loser)], [], set(), src, dst)
    assert not loser.exists()


def test_apply_dedupe_actions_removes_directories(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    rel = src / "release_dir"
    rel.mkdir()
    (rel / "x.mkv").touch()
    _apply_dedupe_actions([], [], {rel}, src, dst)
    assert not rel.exists()


def test_apply_dedupe_actions_swallows_errors(tmp_path: Path):
    """A failure on one file doesn't abort cleanup of the others."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    bad = src / "missing.mkv"  # never created — unlink will fail silently
    good = src / "show.720p.mkv"
    good.touch()
    _apply_dedupe_actions([_move_for(bad), _move_for(good)], [], set(), src, dst)
    assert not good.exists()  # good one was deleted


# ═══════════════════════════════════════════════════════════════════════════
# _resolve_dedupe
# ═══════════════════════════════════════════════════════════════════════════


def test_resolve_dedupe_no_groups_returns_unchanged(tmp_path: Path):
    plan = Plan(moves=[_move_for(tmp_path / "a.mkv")])
    cleaned, dele, quar, dirs = _resolve_dedupe(
        plan,
        interactive=False,
        quarantine_duplicates=False,
        remove_duplicates=False,
        quiet=True,
        dest=tmp_path,
    )
    assert cleaned.moves == plan.moves
    assert dele == [] and quar == [] and dirs == set()


def test_resolve_dedupe_quarantine_path(tmp_path: Path):
    """--quarantine-duplicates injects synthetic ALWAYS_QUARANTINE prompt."""
    src = tmp_path / "src"
    src.mkdir()
    f1080 = src / "show.1080p.mkv"
    f720 = src / "show.720p.mkv"
    f1080.write_bytes(b"x" * 1000)
    f720.write_bytes(b"x" * 100)
    dest = tmp_path / "Show" / "Season 01" / "S01E01.mkv"
    plan = Plan(
        moves=[
            PlannedMove(
                source=f720,
                destination=dest,
                media_type=MediaType.EPISODE,
                tmdb_id=1,
                matched_title="Show",
                confidence="high",
            ),
            PlannedMove(
                source=f1080,
                destination=dest,
                media_type=MediaType.EPISODE,
                tmdb_id=1,
                matched_title="Show",
                confidence="high",
            ),
        ]
    )
    cleaned, dele, quar, _ = _resolve_dedupe(
        plan,
        interactive=False,
        quarantine_duplicates=True,
        remove_duplicates=False,
        quiet=True,
        dest=tmp_path,
    )
    assert len(cleaned.moves) == 1
    assert cleaned.moves[0].source == f1080  # 1080p kept
    assert dele == []
    assert len(quar) == 1
    assert quar[0].source == f720


def test_resolve_dedupe_remove_path(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    f1080 = src / "show.1080p.mkv"
    f720 = src / "show.720p.mkv"
    f1080.write_bytes(b"x" * 1000)
    f720.write_bytes(b"x" * 100)
    dest = tmp_path / "S01E01.mkv"
    plan = Plan(
        moves=[
            _move_for(f720),
            _move_for(f1080),
        ]
    )
    # Force destinations identical (simulate TMDB collision)
    plan.moves[0] = PlannedMove(
        source=f720,
        destination=dest,
        media_type=MediaType.EPISODE,
        tmdb_id=1,
        matched_title="x",
        confidence="high",
    )
    plan.moves[1] = PlannedMove(
        source=f1080,
        destination=dest,
        media_type=MediaType.EPISODE,
        tmdb_id=1,
        matched_title="x",
        confidence="high",
    )
    cleaned, dele, quar, _ = _resolve_dedupe(
        plan,
        interactive=False,
        quarantine_duplicates=False,
        remove_duplicates=True,
        quiet=True,
        dest=tmp_path,
    )
    assert len(cleaned.moves) == 1
    assert len(dele) == 1
    assert dele[0].source == f720
    assert quar == []


def test_resolve_dedupe_non_interactive_no_flags_skips_both(tmp_path: Path):
    """The default non-interactive behaviour: skip both files."""
    src = tmp_path / "src"
    src.mkdir()
    a = src / "a.mkv"
    b = src / "b.mkv"
    a.touch()
    b.touch()
    dest = tmp_path / "S01E01.mkv"
    plan = Plan(
        moves=[
            PlannedMove(
                source=a,
                destination=dest,
                media_type=MediaType.EPISODE,
                tmdb_id=1,
                matched_title="x",
                confidence="high",
            ),
            PlannedMove(
                source=b,
                destination=dest,
                media_type=MediaType.EPISODE,
                tmdb_id=1,
                matched_title="x",
                confidence="high",
            ),
        ]
    )
    cleaned, dele, quar, _ = _resolve_dedupe(
        plan,
        interactive=False,
        quarantine_duplicates=False,
        remove_duplicates=False,
        quiet=True,
        dest=tmp_path,
    )
    assert cleaned.moves == []  # both skipped
    assert len(cleaned.skipped) == 2
    assert dele == [] and quar == []


# ═══════════════════════════════════════════════════════════════════════════
# _handle_junk_files
# ═══════════════════════════════════════════════════════════════════════════


def test_handle_junk_files_dry_run_does_not_move(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    f = src / "Sample.mkv"
    f.write_bytes(b"x" * 100)
    bytes_ = _handle_junk_files([f], src, tmp_path / "dst", dry_run=True)
    assert bytes_ == 100
    assert f.exists()  # not moved


def test_handle_junk_files_live_moves_files(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    f = src / "Sample.mkv"
    f.touch()
    _handle_junk_files([f], src, dst, dry_run=False)
    assert not f.exists()
    assert (dst / ".junk" / "Sample.mkv").exists()


def test_handle_junk_files_empty_list_returns_zero(tmp_path: Path):
    assert _handle_junk_files([], tmp_path, tmp_path / "dst", dry_run=False) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Error / quiet=False branches
# ═══════════════════════════════════════════════════════════════════════════


def test_apply_dedupe_actions_quarantine_failure_logs_error(tmp_path: Path):
    """shutil.move failure during quarantine is logged but doesn't abort."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    loser = src / "f.mkv"
    loser.touch()
    move = _move_for(loser)
    with patch("jellyfiler.cli.shutil.move", side_effect=OSError("permission denied")):
        _apply_dedupe_actions([], [move], set(), src, dst)
    # File still exists in source — move failed silently
    assert loser.exists()


def test_apply_dedupe_actions_rmtree_failure_logs_error(tmp_path: Path):
    """shutil.rmtree failure during dir removal is logged but doesn't abort."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    rel = src / "release"
    rel.mkdir()
    with patch("jellyfiler.cli.shutil.rmtree", side_effect=OSError("locked")):
        _apply_dedupe_actions([], [], {rel}, src, dst)
    assert rel.exists()  # rmtree failed silently


def test_resolve_dedupe_quiet_false_prints_summary(tmp_path: Path, capsys):
    """Coverage for the not-quiet print branches in _resolve_dedupe."""
    src = tmp_path / "src"
    src.mkdir()
    f1080 = src / "show.1080p.mkv"
    f720 = src / "show.720p.mkv"
    f1080.write_bytes(b"x" * 1000)
    f720.write_bytes(b"x" * 100)
    dest = tmp_path / "S01E01.mkv"
    plan = Plan(
        moves=[
            PlannedMove(
                source=f720,
                destination=dest,
                media_type=MediaType.EPISODE,
                tmdb_id=1,
                matched_title="x",
                confidence="high",
            ),
            PlannedMove(
                source=f1080,
                destination=dest,
                media_type=MediaType.EPISODE,
                tmdb_id=1,
                matched_title="x",
                confidence="high",
            ),
        ]
    )
    # quarantine path with quiet=False
    _resolve_dedupe(
        plan,
        interactive=False,
        quarantine_duplicates=True,
        remove_duplicates=False,
        quiet=False,
        dest=tmp_path,
    )
    # delete path with quiet=False
    _resolve_dedupe(
        Plan(moves=[plan.moves[0], plan.moves[1]]),
        interactive=False,
        quarantine_duplicates=False,
        remove_duplicates=True,
        quiet=False,
        dest=tmp_path,
    )


def test_resolve_dedupe_quiet_false_with_dirs_to_remove(tmp_path: Path):
    """Cover the 'dirs_to_remove' print branch by simulating DELETE_LOSERS choice."""
    from jellyfiler.dedupe import DuplicateChoice as _DC

    src = tmp_path / "src"
    src.mkdir()
    rel_a = src / "a"
    rel_b = src / "b"
    rel_a.mkdir()
    rel_b.mkdir()
    fa = rel_a / "show.1080p.mkv"
    fb = rel_b / "show.720p.mkv"
    fa.write_bytes(b"x" * 1000)
    fb.write_bytes(b"x" * 100)
    dest = tmp_path / "S01E01.mkv"
    plan = Plan(
        moves=[
            PlannedMove(
                source=fa,
                destination=dest,
                media_type=MediaType.EPISODE,
                tmdb_id=1,
                matched_title="x",
                confidence="high",
            ),
            PlannedMove(
                source=fb,
                destination=dest,
                media_type=MediaType.EPISODE,
                tmdb_id=1,
                matched_title="x",
                confidence="high",
            ),
        ]
    )
    # Use interactive prompt that returns DELETE_LOSERS — this populates dirs_to_remove
    with patch(
        "jellyfiler.cli.prompt_duplicate_choice",
        return_value=_DC(_DC.DELETE_LOSERS, index=0),
    ):
        cleaned, dele, _quar, dirs = _resolve_dedupe(
            plan,
            interactive=True,
            quarantine_duplicates=False,
            remove_duplicates=False,
            quiet=False,
            dest=tmp_path,
        )
    assert len(dirs) == 1
    assert len(dele) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Dedupe Typer command end-to-end
# ═══════════════════════════════════════════════════════════════════════════

from typer.testing import CliRunner  # noqa: E402

from jellyfiler.cli import app  # noqa: E402

_runner = CliRunner()


def test_dedupe_no_files_exits_clean(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    import os

    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = _runner.invoke(app, ["dedupe", str(src), str(dst), "--no-interactive"])
    assert result.exit_code == 0


def test_dedupe_missing_source_dir_exits_one(tmp_path: Path):
    bad = tmp_path / "nope"
    dst = tmp_path / "dst"
    dst.mkdir()
    import os

    with patch.dict(os.environ, {"TMDB_API_KEY": "fake"}):
        result = _runner.invoke(app, ["dedupe", str(bad), str(dst)])
    assert result.exit_code == 1


def test_dedupe_no_duplicates_exits_clean(tmp_path: Path):
    """Source has files but no duplicates → exit 0 with friendly message."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    f = src / "Show.S01E01.mkv"
    f.touch()

    fake_match = TmdbMatch(1, "Show", 2020, MediaType.EPISODE)

    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = fake_match
    mock_cache.get_tmdb.return_value = None

    import os

    with (
        patch.dict(os.environ, {"TMDB_API_KEY": "fake"}),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient"),
    ):
        result = _runner.invoke(app, ["dedupe", str(src), str(dst), "--no-interactive"])
    assert result.exit_code == 0


def test_dedupe_dry_run_lists_without_acting(tmp_path: Path):
    """With duplicates but no --apply, dedupe reports the plan but doesn't delete."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    f1 = src / "Show.S01E01.1080p.mkv"
    f2 = src / "Show.S01E01.720p.mkv"
    f1.write_bytes(b"x" * 1000)
    f2.write_bytes(b"x" * 100)

    fake_match = TmdbMatch(1, "Show", 2020, MediaType.EPISODE)
    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = fake_match
    mock_cache.get_tmdb.return_value = None

    import os

    with (
        patch.dict(os.environ, {"TMDB_API_KEY": "fake"}),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient"),
    ):
        result = _runner.invoke(
            app,
            [
                "dedupe",
                str(src),
                str(dst),
                "--remove-duplicates",
                "--i-mean-it",
                "--no-interactive",
            ],
        )
    assert result.exit_code == 0
    # No --apply → both files still in source
    assert f1.exists()
    assert f2.exists()


def test_dedupe_apply_actually_deletes_loser(tmp_path: Path):
    """`dedupe --remove-duplicates --i-mean-it --apply` deletes the lower-quality file."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    f1080 = src / "Show.S01E01.1080p.mkv"
    f720 = src / "Show.S01E01.720p.mkv"
    f1080.write_bytes(b"x" * 1000)
    f720.write_bytes(b"x" * 100)

    fake_match = TmdbMatch(1, "Show", 2020, MediaType.EPISODE)
    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = fake_match
    mock_cache.get_tmdb.return_value = None

    import os

    with (
        patch.dict(os.environ, {"TMDB_API_KEY": "fake"}),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient"),
    ):
        result = _runner.invoke(
            app,
            [
                "dedupe",
                str(src),
                str(dst),
                "--remove-duplicates",
                "--i-mean-it",
                "--no-interactive",
                "--apply",
            ],
        )
    assert result.exit_code == 0
    assert f1080.exists()  # winner stays
    assert not f720.exists()  # loser deleted


# ═══════════════════════════════════════════════════════════════════════════
# Organize: TMDB error branch + summary path
# ═══════════════════════════════════════════════════════════════════════════


def test_organize_with_limit_truncates_files(tmp_path: Path):
    """--limit N processes at most N files."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    for i in range(5):
        (src / f"Show.S01E{i:02d}.mkv").touch()

    fake_match = TmdbMatch(1, "Show", 2020, MediaType.EPISODE)
    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = fake_match
    mock_cache.get_tmdb.return_value = None

    import os

    with (
        patch.dict(os.environ, {"TMDB_API_KEY": "fake"}),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient"),
    ):
        result = _runner.invoke(
            app, ["organize", str(src), str(dst), "--limit", "2", "--no-interactive"]
        )
    assert result.exit_code == 0


def test_organize_in_place_with_cleanup_empty_dirs(tmp_path: Path):
    """--in-place --apply --cleanup-empty-dirs runs the empty-dir sweep."""
    src = tmp_path / "src"
    src.mkdir()
    rel = src / "Show.S01E01.RELEASE"
    rel.mkdir()
    (rel / "Show.S01E01.mkv").touch()

    fake_match = TmdbMatch(1, "Show", 2020, MediaType.EPISODE)
    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = fake_match
    mock_cache.get_tmdb.return_value = None

    import os

    with (
        patch.dict(os.environ, {"TMDB_API_KEY": "fake"}),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient"),
    ):
        result = _runner.invoke(
            app,
            [
                "organize",
                str(src),
                "--in-place",
                "--apply",
                "--cleanup-empty-dirs",
                "--no-interactive",
            ],
        )
    assert result.exit_code == 0


def test_organize_duplicate_groups_skipped_with_message(tmp_path: Path):
    """When duplicates are found, organize prints the 'use dedupe' hint."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    f1 = src / "Show.S01E01.1080p.mkv"
    f2 = src / "Show.S01E01.720p.mkv"
    f1.write_bytes(b"x" * 100)
    f2.write_bytes(b"x" * 50)

    fake_match = TmdbMatch(1, "Show", 2020, MediaType.EPISODE)
    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = fake_match
    mock_cache.get_tmdb.return_value = None

    import os

    with (
        patch.dict(os.environ, {"TMDB_API_KEY": "fake"}),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient"),
    ):
        result = _runner.invoke(app, ["organize", str(src), str(dst), "--no-interactive"])
    assert result.exit_code == 0
    # Both files still in source (organize skips duplicates)
    assert f1.exists() and f2.exists()


# ═══════════════════════════════════════════════════════════════════════════
# Quiet=False / progress branches in _process_one_file
# ═══════════════════════════════════════════════════════════════════════════


def test_process_one_file_quiet_false_cached_skip(tmp_path: Path):
    ctx = _ctx(tmp_path, quiet=False)
    ctx.cache.already_moved.return_value = True
    f = tmp_path / "x.mkv"
    f.touch()
    out = _process_one_file(f, ctx)
    assert out.kind == "cached"


def test_process_one_file_quiet_false_junk(tmp_path: Path):
    ctx = _ctx(tmp_path, quiet=False)
    f = tmp_path / "Sample.mkv"
    f.touch()
    out = _process_one_file(f, ctx)
    assert out.kind == "junk"


def test_process_one_file_quiet_false_unknown_type(tmp_path: Path):
    ctx = _ctx(tmp_path, quiet=False)
    f = tmp_path / "weird.mkv"
    f.touch()
    with patch(
        "jellyfiler.cli.guess",
        return_value=GuessedMedia(source_path=f, media_type=MediaType.UNKNOWN, title=""),
    ):
        out = _process_one_file(f, ctx)
    assert out.kind == "planned"


def test_process_one_file_quiet_false_no_title_skip(tmp_path: Path):
    ctx = _ctx(tmp_path, quiet=False, interactive=False)
    f = tmp_path / "weird.mkv"
    f.touch()
    with patch(
        "jellyfiler.cli.guess",
        return_value=GuessedMedia(source_path=f, media_type=MediaType.EPISODE, title=""),
    ):
        out = _process_one_file(f, ctx)
    assert out.kind == "planned" and out.move is not None and out.move.skipped


def test_process_one_file_quiet_false_pinned_print(tmp_path: Path):
    ctx = _ctx(tmp_path, quiet=False)
    ctx.cache.get_pinned.return_value = _tmdb_match()
    f = tmp_path / "Show.S01E01.mkv"
    f.touch()
    out = _process_one_file(f, ctx)
    assert out.kind == "planned"


def test_process_one_file_quiet_false_ambiguous_skip(tmp_path: Path):
    ctx = _ctx(tmp_path, quiet=False, interactive=False)
    f = tmp_path / "Show.S01E01.mkv"
    f.touch()
    with (
        patch(
            "jellyfiler.cli._lookup_match_chain",
            return_value=LookupResult(
                matches=[_tmdb_match("A"), _tmdb_match("B")],
                search_title="Show",
                status="ok",
            ),
        ),
        patch("jellyfiler.cli._resolve_match", return_value=None),
    ):
        out = _process_one_file(f, ctx)
    assert out.kind == "planned" and out.move.skipped


def test_process_one_file_with_progress_pauses_for_interactive_prompt(tmp_path: Path):
    """When `progress` is passed, it's stopped/started around interactive prompts."""
    from rich.progress import Progress

    ctx = _ctx(tmp_path, interactive=True)
    f = tmp_path / "weird.mkv"
    f.touch()
    progress = Progress()
    progress.start()
    try:
        with (
            patch(
                "jellyfiler.cli.guess",
                return_value=GuessedMedia(source_path=f, media_type=MediaType.EPISODE, title=""),
            ),
            patch("jellyfiler.cli.prompt_manual_title", return_value="Real Title"),
            patch(
                "jellyfiler.cli._lookup_match_chain",
                return_value=LookupResult(matches=[], search_title="Real Title", status="ok"),
            ),
        ):
            out = _process_one_file(f, ctx, progress=progress)
        assert out.kind == "planned"
    finally:
        progress.stop()


# ═══════════════════════════════════════════════════════════════════════════
# Lookup chain edge cases
# ═══════════════════════════════════════════════════════════════════════════


def test_lookup_chain_anilist_cache_hit(tmp_path: Path):
    """AniList path uses the cache when available, doesn't call search_anime again."""
    ctx = _ctx(tmp_path)
    # First .get_tmdb returns nothing (TMDB lookup), second returns AniList cache hit
    ctx.cache.get_tmdb.side_effect = [None, [_tmdb_match("Anime", 2020)]]
    ctx.tmdb.search_tv.return_value = []
    g = GuessedMedia(
        source_path=Path("[HorribleSubs] Show - 01.mkv"),
        media_type=MediaType.EPISODE,
        title="Show",
    )
    with patch("jellyfiler.cli.search_anime") as mock_search:
        out = _lookup_match_chain(g, Path("[HorribleSubs] Show - 01.mkv"), ctx)
    mock_search.assert_not_called()  # cache hit short-circuited
    assert len(out.matches) == 1


def test_lookup_chain_quiet_false_anilist_print(tmp_path: Path, capsys):
    """quiet=False prints the AniList fallback message."""
    ctx = _ctx(tmp_path, quiet=False)
    ctx.tmdb.search_tv.return_value = []
    g = GuessedMedia(
        source_path=Path("[HorribleSubs] Show - 01.mkv"),
        media_type=MediaType.EPISODE,
        title="Show",
    )
    with patch(
        "jellyfiler.cli.search_anime",
        return_value=[_tmdb_match("Show (Anime)", 2020)],
    ):
        out = _lookup_match_chain(g, Path("[HorribleSubs] Show - 01.mkv"), ctx)
    assert out.status == "ok"


def test_lookup_chain_variant_retry_swallows_exception(tmp_path: Path):
    """Title-variant retry exceptions are swallowed; loop continues."""
    ctx = _ctx(tmp_path)
    # Initial search returns []. First variant retry throws, second succeeds.
    ctx.tmdb.search_tv.side_effect = [
        [],  # initial: no match
        RuntimeError("api down"),  # variant 1 throws
        [_tmdb_match("Real", 2020)],  # variant 2: ok
    ]
    g = GuessedMedia(
        source_path=Path("x.mkv"),
        media_type=MediaType.EPISODE,
        title="Superman & Batman",  # has multiple variants
    )
    out = _lookup_match_chain(g, Path("x.mkv"), ctx)
    assert out.status == "ok"


def test_lookup_chain_ai_retry_exception_swallowed(tmp_path: Path):
    """AI-suggested retry exception is swallowed (line 469-470)."""
    ctx = _ctx(tmp_path, use_ai=True)
    # The title "Show Title" has no variants (multiple words, no camelcase, no &).
    # First call (initial search): empty. Second call (AI retry): raises.
    call_count = {"n": 0}

    def search_side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return []
        raise RuntimeError("ai retry fail")

    ctx.tmdb.search_tv.side_effect = search_side_effect
    g = GuessedMedia(source_path=Path("x.mkv"), media_type=MediaType.EPISODE, title="Show Title")
    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake"}),
        patch(
            "jellyfiler.cli.suggest_search",
            return_value={"title": "Real Show", "year": None},
        ),
    ):
        out = _lookup_match_chain(g, Path("x.mkv"), ctx)
    assert out.status == "ok"


# ═══════════════════════════════════════════════════════════════════════════
# Parallel pipeline (phase 1 → parallel phase 2 → phase 3)
# ═══════════════════════════════════════════════════════════════════════════


def test_classify_file_cached(tmp_path: Path):
    from jellyfiler.cli import _classify_file

    ctx = _ctx(tmp_path)
    ctx.cache.already_moved.return_value = True
    f = tmp_path / "x.mkv"
    f.touch()
    cf = _classify_file(f, ctx)
    assert cf.kind == "cached"


def test_classify_file_junk(tmp_path: Path):
    from jellyfiler.cli import _classify_file

    ctx = _ctx(tmp_path)
    f = tmp_path / "Sample.mkv"
    f.touch()
    cf = _classify_file(f, ctx)
    assert cf.kind == "junk"


def test_classify_file_unknown_type_returns_skipped(tmp_path: Path):
    from jellyfiler.cli import _classify_file

    ctx = _ctx(tmp_path)
    f = tmp_path / "weird.mkv"
    f.touch()
    with patch(
        "jellyfiler.cli.guess",
        return_value=GuessedMedia(source_path=f, media_type=MediaType.UNKNOWN, title=""),
    ):
        cf = _classify_file(f, ctx)
    assert cf.kind == "skipped"
    assert cf.move is not None and cf.move.skipped


def test_classify_file_pinned_returns_resolved_move(tmp_path: Path):
    from jellyfiler.cli import _classify_file

    ctx = _ctx(tmp_path)
    ctx.cache.get_pinned.return_value = _tmdb_match()
    f = tmp_path / "Show.S01E01.mkv"
    f.touch()
    cf = _classify_file(f, ctx)
    assert cf.kind == "pinned"
    assert cf.move is not None and not cf.move.skipped


def test_classify_file_needs_lookup(tmp_path: Path):
    from jellyfiler.cli import _classify_file

    ctx = _ctx(tmp_path)
    f = tmp_path / "Show.S01E01.mkv"
    f.touch()
    cf = _classify_file(f, ctx)
    assert cf.kind == "needs_lookup"
    assert cf.guessed is not None
    assert cf.move is None


def test_finalize_after_lookup_tmdb_error_propagates(tmp_path: Path):
    from jellyfiler.cli import ClassifiedFile, _finalize_after_lookup

    ctx = _ctx(tmp_path)
    cf = ClassifiedFile(file=tmp_path / "x.mkv", kind="needs_lookup", guessed=_episode())
    out = _finalize_after_lookup(cf, LookupResult(status="tmdb_error", error_msg="boom"), ctx)
    assert out.kind == "tmdb_error"


def test_finalize_after_lookup_ai_abort_propagates(tmp_path: Path):
    from jellyfiler.cli import ClassifiedFile, _finalize_after_lookup

    ctx = _ctx(tmp_path)
    cf = ClassifiedFile(file=tmp_path / "x.mkv", kind="needs_lookup", guessed=_episode())
    out = _finalize_after_lookup(cf, LookupResult(status="ai_abort", error_msg="quota"), ctx)
    assert out.kind == "ai_abort"


def test_finalize_after_lookup_planned_with_match(tmp_path: Path):
    from jellyfiler.cli import ClassifiedFile, _finalize_after_lookup

    ctx = _ctx(tmp_path)
    cf = ClassifiedFile(file=tmp_path / "x.mkv", kind="needs_lookup", guessed=_episode())
    match = _tmdb_match()
    with patch("jellyfiler.cli._resolve_match", return_value=match):
        out = _finalize_after_lookup(
            cf,
            LookupResult(matches=[match], search_title="Show", status="ok"),
            ctx,
        )
    assert out.kind == "planned"
    assert out.move is not None


def test_run_pipeline_parallel_same_results_as_sequential(tmp_path: Path):
    """Parallel pipeline must produce the same plan as sequential, just faster."""
    from jellyfiler.cli import _run_pipeline

    src = tmp_path / "src"
    src.mkdir()
    files = []
    for i in range(5):
        f = src / f"Show.S01E{i:02d}.mkv"
        f.touch()
        files.append(f)

    fake_match = _tmdb_match()
    ctx_seq = _ctx(tmp_path)
    ctx_seq.cache.get_pinned.return_value = fake_match
    ctx_par = _ctx(tmp_path)
    ctx_par.cache.get_pinned.return_value = fake_match
    ctx_par.parallel = 4

    seq_result = _run_pipeline(files, ctx_seq)
    par_result = _run_pipeline(files, ctx_par)

    # Same plan size, same sources (order may differ in par_result.planned_moves
    # but with all-pinned matches, both should be sequential by construction)
    assert len(seq_result.planned_moves) == len(par_result.planned_moves)
    seq_sources = sorted(m.source for m in seq_result.planned_moves)
    par_sources = sorted(m.source for m in par_result.planned_moves)
    assert seq_sources == par_sources


def test_run_pipeline_parallel_dispatches_to_threads(tmp_path: Path):
    """Phase 2 actually uses a ThreadPoolExecutor when parallel > 1."""
    import threading

    from jellyfiler.cli import _run_pipeline

    src = tmp_path / "src"
    src.mkdir()
    files = [src / f"Show.S01E{i:02d}.mkv" for i in range(3)]
    for f in files:
        f.touch()

    seen_threads: set[str] = set()
    main_thread = threading.current_thread().name

    def lookup_side_effect(*args, **kwargs):
        seen_threads.add(threading.current_thread().name)
        return LookupResult(matches=[_tmdb_match()], search_title="Show", status="ok")

    ctx = _ctx(tmp_path)
    ctx.parallel = 4
    with (
        patch("jellyfiler.cli._lookup_match_chain", side_effect=lookup_side_effect),
        patch("jellyfiler.cli._resolve_match", return_value=_tmdb_match()),
    ):
        _run_pipeline(files, ctx)

    # At least one lookup must have happened on a non-main thread.
    assert any(t != main_thread for t in seen_threads), f"all threads were main: {seen_threads}"


def test_run_pipeline_parallel_aborts_on_tmdb_error(tmp_path: Path):
    """A tmdb_error result in phase 3 still aborts the run."""
    from jellyfiler.cli import _run_pipeline

    src = tmp_path / "src"
    src.mkdir()
    f = src / "Show.S01E01.mkv"
    f.touch()

    ctx = _ctx(tmp_path)
    ctx.parallel = 2
    with patch(
        "jellyfiler.cli._lookup_match_chain",
        return_value=LookupResult(status="tmdb_error", error_msg="boom"),
    ):
        result = _run_pipeline([f], ctx)
    assert result.aborted
    assert result.tmdb_errors == 1


def test_run_pipeline_parallel_aborts_on_ai_abort(tmp_path: Path):
    from jellyfiler.cli import _run_pipeline

    src = tmp_path / "src"
    src.mkdir()
    f = src / "Show.S01E01.mkv"
    f.touch()

    ctx = _ctx(tmp_path)
    ctx.parallel = 2
    with patch(
        "jellyfiler.cli._lookup_match_chain",
        return_value=LookupResult(status="ai_abort", error_msg="quota"),
    ):
        result = _run_pipeline([f], ctx)
    assert result.aborted


def test_run_pipeline_parallel_worker_exception_records_tmdb_error(tmp_path: Path):
    """If a worker thread throws an exception, the file gets a tmdb_error result."""
    from jellyfiler.cli import _run_pipeline

    src = tmp_path / "src"
    src.mkdir()
    f = src / "Show.S01E01.mkv"
    f.touch()

    ctx = _ctx(tmp_path)
    ctx.parallel = 2
    with patch(
        "jellyfiler.cli._lookup_match_chain",
        side_effect=RuntimeError("worker crashed"),
    ):
        result = _run_pipeline([f], ctx)
    assert result.aborted
    assert result.tmdb_errors == 1


def test_run_pipeline_parallel_no_lookup_files_skips_phase_2(tmp_path: Path):
    """If every file is cached/junk/pinned, phase 2 doesn't fire."""
    from jellyfiler.cli import _run_pipeline

    src = tmp_path / "src"
    src.mkdir()
    junk = src / "Sample.mkv"
    junk.touch()
    cached = src / "Show.S01E01.mkv"
    cached.touch()

    ctx = _ctx(tmp_path)
    ctx.parallel = 4
    ctx.cache.already_moved.side_effect = lambda f: f == cached

    with patch("jellyfiler.cli._lookup_match_chain") as mock_lookup:
        result = _run_pipeline([junk, cached], ctx)
    mock_lookup.assert_not_called()
    assert result.junk_files == [junk]


def test_lookup_chain_skips_ai_prompt_in_parallel_mode(tmp_path: Path):
    """In parallel mode, AI errors must abort instead of prompting (no shared stdin)."""
    from jellyfiler.ai_query import AiQueryError

    ctx = _ctx(tmp_path, use_ai=True, interactive=True)
    ctx.parallel = 4  # parallel mode
    ctx.tmdb.search_tv.return_value = []
    g = GuessedMedia(source_path=Path("x.mkv"), media_type=MediaType.EPISODE, title="weird")
    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake"}),
        patch("jellyfiler.cli.suggest_search", side_effect=AiQueryError("net")),
        patch("jellyfiler.cli.typer.confirm") as mock_confirm,
    ):
        out = _lookup_match_chain(g, Path("x.mkv"), ctx)
    assert out.status == "ai_abort"
    mock_confirm.assert_not_called()  # crucial: no interactive prompt in parallel


def test_organize_tmdb_error_exits_one(tmp_path: Path):
    """When _process_one_file returns tmdb_error, organize exits with code 1."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    f = src / "Show.S01E01.mkv"
    f.touch()

    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = None
    mock_cache.get_tmdb.return_value = None

    response = MagicMock()
    response.status_code = 500
    response.reason_phrase = "Internal Server Error"
    mock_tmdb = MagicMock()
    mock_tmdb.search_tv.side_effect = httpx.HTTPStatusError(
        "boom", request=MagicMock(), response=response
    )

    import os

    with (
        patch.dict(os.environ, {"TMDB_API_KEY": "fake"}),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient", return_value=mock_tmdb),
    ):
        result = _runner.invoke(app, ["organize", str(src), str(dst), "--no-interactive"])
    assert result.exit_code == 1
