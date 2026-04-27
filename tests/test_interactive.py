"""Tests for interactive prompt helpers."""

from unittest.mock import patch

from jellyfiler.interactive import (
    prompt_episode_number,
    prompt_manual_title,
    prompt_tmdb_match,
)
from jellyfiler.models import MediaType, TmdbMatch

_EPISODES = [(1, "Space Pilot 3000"), (2, "The Series Has Landed"), (3, "I, Roommate")]


def _tmdb(title: str, year: int | None = 2000, tmdb_id: int = 1) -> TmdbMatch:
    return TmdbMatch(tmdb_id=tmdb_id, title=title, year=year, media_type=MediaType.EPISODE)


def test_prompt_episode_number_valid_choice():
    with patch("jellyfiler.interactive.typer.prompt", return_value="2"):
        result = prompt_episode_number("episode.mkv", _EPISODES)
    assert result == 2  # episode number from the list, not the index


def test_prompt_episode_number_first_item():
    with patch("jellyfiler.interactive.typer.prompt", return_value="1"):
        result = prompt_episode_number("episode.mkv", _EPISODES)
    assert result == 1


def test_prompt_episode_number_skip_zero():
    with patch("jellyfiler.interactive.typer.prompt", return_value="0"):
        result = prompt_episode_number("episode.mkv", _EPISODES)
    assert result is None


def test_prompt_episode_number_skip_empty():
    with patch("jellyfiler.interactive.typer.prompt", return_value=""):
        result = prompt_episode_number("episode.mkv", _EPISODES)
    assert result is None


def test_prompt_episode_number_invalid_input():
    with patch("jellyfiler.interactive.typer.prompt", return_value="abc"):
        result = prompt_episode_number("episode.mkv", _EPISODES)
    assert result is None


def test_prompt_episode_number_out_of_range():
    with patch("jellyfiler.interactive.typer.prompt", return_value="99"):
        result = prompt_episode_number("episode.mkv", _EPISODES)
    assert result is None


def test_prompt_episode_number_returns_episode_num_not_index():
    """Episode list starting at E05 — choice 1 should return 5, not 1."""
    episodes = [(5, "Fifth Episode"), (6, "Sixth Episode")]
    with patch("jellyfiler.interactive.typer.prompt", return_value="1"):
        result = prompt_episode_number("episode.mkv", episodes)
    assert result == 5


# ---------------------------------------------------------------------------
# prompt_tmdb_match
# ---------------------------------------------------------------------------


def test_prompt_tmdb_match_auto_selects_single_result():
    """When TMDB returns exactly one result, it's auto-selected without a prompt."""
    matches = [_tmdb("Coco", 2017)]
    # No prompt patch — if it tried to prompt, the test would block forever
    result = prompt_tmdb_match("file.mkv", "Coco", matches, MediaType.MOVIE)
    assert result is matches[0]


def test_prompt_tmdb_match_valid_choice():
    matches = [_tmdb("Avatar", 2009, 1), _tmdb("Avatar: The Last Airbender", 2005, 2)]
    with patch("jellyfiler.interactive.typer.prompt", return_value="2"):
        result = prompt_tmdb_match("file.mkv", "Avatar", matches, MediaType.EPISODE)
    assert result is not None
    assert result.tmdb_id == 2


def test_prompt_tmdb_match_skip_zero():
    matches = [_tmdb("A", 2020, 1), _tmdb("B", 2021, 2)]
    with patch("jellyfiler.interactive.typer.prompt", return_value="0"):
        result = prompt_tmdb_match("file.mkv", "A", matches, MediaType.EPISODE)
    assert result is None


def test_prompt_tmdb_match_skip_empty():
    matches = [_tmdb("A", 2020, 1), _tmdb("B", 2021, 2)]
    with patch("jellyfiler.interactive.typer.prompt", return_value=""):
        result = prompt_tmdb_match("file.mkv", "A", matches, MediaType.EPISODE)
    assert result is None


def test_prompt_tmdb_match_invalid_input():
    matches = [_tmdb("A", 2020, 1), _tmdb("B", 2021, 2)]
    with patch("jellyfiler.interactive.typer.prompt", return_value="abc"):
        result = prompt_tmdb_match("file.mkv", "A", matches, MediaType.EPISODE)
    assert result is None


def test_prompt_tmdb_match_out_of_range():
    matches = [_tmdb("A", 2020, 1), _tmdb("B", 2021, 2)]
    with patch("jellyfiler.interactive.typer.prompt", return_value="99"):
        result = prompt_tmdb_match("file.mkv", "A", matches, MediaType.EPISODE)
    assert result is None


def test_prompt_tmdb_match_caps_display_at_ten():
    """More than 10 results — only the first 10 are shown, choice 10 picks index 9."""
    matches = [_tmdb(f"Show {i}", 2000 + i, tmdb_id=i) for i in range(15)]
    with patch("jellyfiler.interactive.typer.prompt", return_value="10"):
        result = prompt_tmdb_match("file.mkv", "Show", matches, MediaType.EPISODE)
    assert result is not None
    assert result.tmdb_id == 9  # 10th item = matches[9]


# ---------------------------------------------------------------------------
# prompt_manual_title
# ---------------------------------------------------------------------------


def test_prompt_manual_title_returns_input():
    with patch("jellyfiler.interactive.typer.prompt", return_value="The Real Title"):
        result = prompt_manual_title("messy filename.mkv", "")
    assert result == "The Real Title"


def test_prompt_manual_title_strips_whitespace():
    with patch("jellyfiler.interactive.typer.prompt", return_value="  Trimmed  "):
        result = prompt_manual_title("file.mkv", "")
    assert result == "Trimmed"


def test_prompt_manual_title_empty_returns_none():
    with patch("jellyfiler.interactive.typer.prompt", return_value=""):
        result = prompt_manual_title("file.mkv", "")
    assert result is None


def test_prompt_manual_title_whitespace_only_returns_none():
    with patch("jellyfiler.interactive.typer.prompt", return_value="   "):
        result = prompt_manual_title("file.mkv", "")
    assert result is None
