"""Tests for interactive prompt helpers."""

from unittest.mock import patch

from jellyfiler.interactive import prompt_episode_number

_EPISODES = [(1, "Space Pilot 3000"), (2, "The Series Has Landed"), (3, "I, Roommate")]


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
