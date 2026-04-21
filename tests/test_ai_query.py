"""Tests for the Claude Haiku AI search query fallback."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from jellyfiler.ai_query import (
    _SYSTEM_MOVIE,
    _SYSTEM_TV,
    AiQueryError,
    AiUsage,
    preflight_check,
    suggest_search,
)


def _make_response(text: str) -> MagicMock:
    msg = MagicMock()
    block = anthropic.types.TextBlock(type="text", text=text)
    msg.content = [block]
    msg.usage.input_tokens = 10
    msg.usage.output_tokens = 5
    return msg


def _mock_anthropic(response_text: str) -> MagicMock:
    mock_module = MagicMock()
    mock_module.Anthropic.return_value.messages.create.return_value = _make_response(response_text)
    return mock_module


def test_movie_prompt_used_for_movies():
    mock = _mock_anthropic(json.dumps({"title": "Blade Runner 2049", "year": 2017}))
    with (
        patch("jellyfiler.ai_query._anthropic", mock),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        suggest_search("folder", "file.mkv", "fake-key", is_tv=False)
    _, kwargs = mock.Anthropic.return_value.messages.create.call_args
    assert kwargs["system"] == _SYSTEM_MOVIE


def test_tv_prompt_used_for_episodes():
    mock = _mock_anthropic(json.dumps({"title": "Futurama"}))
    with (
        patch("jellyfiler.ai_query._anthropic", mock),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        suggest_search("Futurama.S12.1080p", "E03.mkv", "fake-key", is_tv=True)
    _, kwargs = mock.Anthropic.return_value.messages.create.call_args
    assert kwargs["system"] == _SYSTEM_TV


def test_movie_returns_title_and_year():
    payload = {"title": "Blade Runner 2049", "year": 2017}
    with (
        patch("jellyfiler.ai_query._anthropic", _mock_anthropic(json.dumps(payload))),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result, usage = suggest_search("folder", "file.mkv", "fake-key", is_tv=False)
    assert result is not None
    assert result["title"] == "Blade Runner 2049"
    assert result["year"] == 2017
    assert usage.input_tokens == 10
    assert usage.output_tokens == 5


def test_tv_returns_title_only():
    payload = {"title": "Futurama"}
    with (
        patch("jellyfiler.ai_query._anthropic", _mock_anthropic(json.dumps(payload))),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result, usage = suggest_search("Futurama.S12.1080p", "E03.mkv", "fake-key", is_tv=True)
    assert result is not None
    assert result["title"] == "Futurama"
    assert usage.input_tokens > 0


def test_suggest_search_strips_markdown_fences():
    payload = {"title": "Blade Runner 2049", "year": 2017}
    wrapped = f"```json\n{json.dumps(payload)}\n```"
    with (
        patch("jellyfiler.ai_query._anthropic", _mock_anthropic(wrapped)),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result, _ = suggest_search("folder", "file.mkv", "fake-key")
    assert result is not None
    assert result["title"] == "Blade Runner 2049"


def test_suggest_search_raises_on_api_error():
    mock_module = MagicMock()
    mock_module.Anthropic.return_value.messages.create.side_effect = Exception("network error")
    with (
        patch("jellyfiler.ai_query._anthropic", mock_module),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
        pytest.raises(AiQueryError),
    ):
        suggest_search("folder", "file.mkv", "fake-key")


def test_suggest_search_returns_none_on_invalid_json():
    with (
        patch("jellyfiler.ai_query._anthropic", _mock_anthropic("not json at all")),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result, usage = suggest_search("folder", "file.mkv", "fake-key")
    assert result is None
    assert usage.input_tokens == 10


def test_suggest_search_returns_none_when_title_missing():
    payload = {"year": 2020}
    with (
        patch("jellyfiler.ai_query._anthropic", _mock_anthropic(json.dumps(payload))),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result, usage = suggest_search("folder", "file.mkv", "fake-key")
    assert result is None
    assert usage.input_tokens == 10


def test_suggest_search_returns_none_when_anthropic_not_available():
    with patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", False):
        result, usage = suggest_search("folder", "file.mkv", "fake-key")
    assert result is None
    assert usage == (0, 0)


def test_preflight_check_returns_true_on_success():
    mock = _mock_anthropic("true")
    with (
        patch("jellyfiler.ai_query._anthropic", mock),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        assert preflight_check("fake-key") is True


def test_preflight_check_returns_false_on_wrong_response():
    mock = _mock_anthropic("hello there")
    with (
        patch("jellyfiler.ai_query._anthropic", mock),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        assert preflight_check("fake-key") is False


def test_preflight_check_returns_false_on_api_error():
    mock_module = MagicMock()
    mock_module.Anthropic.return_value.messages.create.side_effect = Exception("auth failed")
    with (
        patch("jellyfiler.ai_query._anthropic", mock_module),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        assert preflight_check("bad-key") is False


def test_tmdb_error_stops_the_run(tmp_path: Path):
    """Any TMDB error must abort the loop — no further files processed."""
    import httpx
    from typer.testing import CliRunner

    from jellyfiler.cli import app
    from jellyfiler.guesser import GuessedMedia
    from jellyfiler.models import MediaType

    fake_file = tmp_path / "Movie.2020.mkv"
    fake_file.touch()

    guessed = GuessedMedia(
        source_path=fake_file,
        media_type=MediaType.MOVIE,
        title="Movie",
        year=2020,
    )

    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = None
    mock_cache.get_tmdb.return_value = None

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.reason_phrase = "Unauthorized"
    mock_tmdb = MagicMock()
    mock_tmdb.search_movie.side_effect = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=mock_response
    )

    with (
        patch("jellyfiler.cli.find_media_files", return_value=[fake_file]),
        patch("jellyfiler.cli.guess", return_value=guessed),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient", return_value=mock_tmdb),
        patch.dict(os.environ, {"TMDB_API_KEY": "fake"}),
    ):
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["organize", str(tmp_path), str(tmp_path / "dest"), "--no-interactive"],
            catch_exceptions=False,
        )

    assert result.exit_code != 0 or "stopping" in result.output.lower()


def test_ai_error_stops_non_interactive_run(tmp_path: Path):
    """AiQueryError in non-interactive mode must abort the run."""
    from typer.testing import CliRunner

    from jellyfiler.cli import app
    from jellyfiler.guesser import GuessedMedia
    from jellyfiler.models import MediaType

    fake_file = tmp_path / "Unknown.2020.mkv"
    fake_file.touch()

    guessed = GuessedMedia(
        source_path=fake_file,
        media_type=MediaType.MOVIE,
        title="Unknown",
        year=2020,
    )

    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = None
    mock_cache.get_tmdb.return_value = None

    mock_tmdb = MagicMock()
    mock_tmdb.search_movie.return_value = []

    with (
        patch("jellyfiler.cli.preflight_check", return_value=True),
        patch("jellyfiler.cli.find_media_files", return_value=[fake_file]),
        patch("jellyfiler.cli.guess", return_value=guessed),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient", return_value=mock_tmdb),
        patch("jellyfiler.cli.best_match", return_value=None),
        patch("jellyfiler.cli.suggest_search", side_effect=AiQueryError("quota exceeded")),
        patch.dict(os.environ, {"TMDB_API_KEY": "fake", "ANTHROPIC_API_KEY": "fake-ai-key"}),
    ):
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["organize", str(tmp_path), str(tmp_path / "dest"), "--use-ai", "--no-interactive"],
            catch_exceptions=False,
        )

    assert "quota exceeded" in result.output or "Stopping" in result.output


def test_use_ai_aborts_when_key_missing():
    """--use-ai with no ANTHROPIC_API_KEY must exit with an error before scanning."""
    from typer.testing import CliRunner

    from jellyfiler.cli import app

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        # Still need TMDB_API_KEY so the client init doesn't fail first
        os.environ["TMDB_API_KEY"] = "fake"
        runner = CliRunner()
        result = runner.invoke(app, ["organize", "/fake/src", "/fake/dest", "--use-ai"])
    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in result.output or (
        result.stdout and "ANTHROPIC_API_KEY" in result.stdout
    )


def test_use_ai_aborts_when_preflight_fails():
    """--use-ai must abort when Haiku doesn't respond 'true'."""
    from typer.testing import CliRunner

    from jellyfiler.cli import app

    with (
        patch("jellyfiler.cli.preflight_check", return_value=False),
        patch.dict(os.environ, {"TMDB_API_KEY": "fake", "ANTHROPIC_API_KEY": "bad-key"}),
    ):
        runner = CliRunner()
        result = runner.invoke(app, ["organize", "/fake/src", "/fake/dest", "--use-ai"])
    assert result.exit_code != 0


def test_use_ai_flag_gates_ai_call():
    """suggest_search must NOT be called when use_ai=False (the default)."""
    with patch("jellyfiler.cli.suggest_search") as mock_suggest:
        with patch("jellyfiler.cli.find_media_files", return_value=[]):
            from typer.testing import CliRunner

            from jellyfiler.cli import app

            runner = CliRunner()
            runner.invoke(app, ["organize", "/fake/src", "/fake/dest"], catch_exceptions=False)
        mock_suggest.assert_not_called()


def test_use_ai_flag_true_calls_suggest_search(tmp_path: Path):
    """suggest_search MUST be called when --use-ai is passed and TMDB finds nothing."""
    from typer.testing import CliRunner

    from jellyfiler.cli import app
    from jellyfiler.guesser import GuessedMedia
    from jellyfiler.models import MediaType

    fake_file = tmp_path / "Unknown.Title.2020.mkv"
    fake_file.touch()

    guessed = GuessedMedia(
        source_path=fake_file,
        media_type=MediaType.MOVIE,
        title="Unknown Title",
        year=2020,
    )

    mock_cache = MagicMock()
    mock_cache.already_moved.return_value = False
    mock_cache.get_pinned.return_value = None
    mock_cache.get_tmdb.return_value = None

    mock_tmdb = MagicMock()
    mock_tmdb.search_movie.return_value = []

    with (
        patch("jellyfiler.cli.preflight_check", return_value=True),
        patch("jellyfiler.cli.find_media_files", return_value=[fake_file]),
        patch("jellyfiler.cli.guess", return_value=guessed),
        patch("jellyfiler.cli.Cache", return_value=mock_cache),
        patch("jellyfiler.cli.TmdbClient", return_value=mock_tmdb),
        patch("jellyfiler.cli.best_match", return_value=None),
        patch("jellyfiler.cli.suggest_search", return_value=(None, AiUsage(0, 0))) as mock_suggest,
        patch.dict(os.environ, {"TMDB_API_KEY": "fake", "ANTHROPIC_API_KEY": "fake-ai-key"}),
    ):
        runner = CliRunner()
        runner.invoke(
            app,
            ["organize", str(tmp_path), str(tmp_path / "dest"), "--use-ai", "--no-interactive"],
            catch_exceptions=False,
        )

    mock_suggest.assert_called_once()
