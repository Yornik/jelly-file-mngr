"""Tests for the Claude Haiku AI search query fallback."""

import json
from unittest.mock import MagicMock, patch

import anthropic

from jellyfiler.ai_query import _SYSTEM_MOVIE, _SYSTEM_TV, suggest_search


def _make_response(text: str) -> MagicMock:
    msg = MagicMock()
    block = anthropic.types.TextBlock(type="text", text=text)
    msg.content = [block]
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
        result = suggest_search("folder", "file.mkv", "fake-key", is_tv=False)
    assert result is not None
    assert result["title"] == "Blade Runner 2049"
    assert result["year"] == 2017


def test_tv_returns_title_only():
    payload = {"title": "Futurama"}
    with (
        patch("jellyfiler.ai_query._anthropic", _mock_anthropic(json.dumps(payload))),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result = suggest_search("Futurama.S12.1080p", "E03.mkv", "fake-key", is_tv=True)
    assert result is not None
    assert result["title"] == "Futurama"


def test_suggest_search_strips_markdown_fences():
    payload = {"title": "Blade Runner 2049", "year": 2017}
    wrapped = f"```json\n{json.dumps(payload)}\n```"
    with (
        patch("jellyfiler.ai_query._anthropic", _mock_anthropic(wrapped)),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result = suggest_search("folder", "file.mkv", "fake-key")
    assert result is not None
    assert result["title"] == "Blade Runner 2049"


def test_suggest_search_returns_none_on_api_error():
    mock_module = MagicMock()
    mock_module.Anthropic.return_value.messages.create.side_effect = Exception("network error")
    with (
        patch("jellyfiler.ai_query._anthropic", mock_module),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result = suggest_search("folder", "file.mkv", "fake-key")
    assert result is None


def test_suggest_search_returns_none_on_invalid_json():
    with (
        patch("jellyfiler.ai_query._anthropic", _mock_anthropic("not json at all")),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result = suggest_search("folder", "file.mkv", "fake-key")
    assert result is None


def test_suggest_search_returns_none_when_title_missing():
    payload = {"year": 2020}
    with (
        patch("jellyfiler.ai_query._anthropic", _mock_anthropic(json.dumps(payload))),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result = suggest_search("folder", "file.mkv", "fake-key")
    assert result is None


def test_suggest_search_returns_none_when_anthropic_not_available():
    with patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", False):
        result = suggest_search("folder", "file.mkv", "fake-key")
    assert result is None
