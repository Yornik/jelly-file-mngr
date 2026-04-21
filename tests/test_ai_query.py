"""Tests for the Claude Haiku AI search query fallback."""

import json
from unittest.mock import MagicMock, patch

from jellyfiler.ai_query import suggest_search


def _make_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


def _mock_anthropic(response_text: str) -> MagicMock:
    mock_module = MagicMock()
    mock_module.Anthropic.return_value.messages.create.return_value = _make_response(response_text)
    return mock_module


def test_suggest_search_returns_parsed_dict():
    payload = {"title": "Futurama", "year": None, "media_type": "episode"}
    with (
        patch("jellyfiler.ai_query._anthropic", _mock_anthropic(json.dumps(payload))),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result = suggest_search("Futurama.S12.1080p", "E03.mkv", "fake-key")
    assert result is not None
    assert result["title"] == "Futurama"
    assert result["year"] is None
    assert result["media_type"] == "episode"


def test_suggest_search_strips_markdown_fences():
    payload = {"title": "Blade Runner 2049", "year": 2017, "media_type": "movie", "season": None}
    wrapped = f"```json\n{json.dumps(payload)}\n```"
    with (
        patch("jellyfiler.ai_query._anthropic", _mock_anthropic(wrapped)),
        patch("jellyfiler.ai_query._ANTHROPIC_AVAILABLE", True),
    ):
        result = suggest_search("folder", "file.mkv", "fake-key")
    assert result is not None
    assert result["title"] == "Blade Runner 2049"
    assert result["year"] == 2017


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
    payload = {"year": 2020, "media_type": "movie"}
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
