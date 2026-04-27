"""Tests for TMDB title matching and HTTP client wrappers."""

import httpx
import pytest

from jellyfiler.models import MediaType, TmdbMatch
from jellyfiler.tmdb import TmdbClient, _norm, best_match

# ---------------------------------------------------------------------------
# _norm — case folding + accent stripping
# ---------------------------------------------------------------------------


def test_norm_lowercases():
    assert _norm("Pokemon") == "pokemon"


def test_norm_strips_accents():
    """é, ó, ñ etc. collapse to their ASCII form so 'Pokémon' matches 'Pokemon'."""
    assert _norm("Pokémon") == "pokemon"
    assert _norm("Café") == "cafe"
    assert _norm("Naïve") == "naive"


def test_norm_preserves_punctuation():
    """Punctuation isn't stripped — only diacritics are."""
    assert _norm("Avatar: The Last Airbender") == "avatar: the last airbender"


# ---------------------------------------------------------------------------
# best_match — the 4 matching tiers
# ---------------------------------------------------------------------------


def _match(title: str, year: int | None = 2000, tmdb_id: int = 1) -> TmdbMatch:
    return TmdbMatch(tmdb_id=tmdb_id, title=title, year=year, media_type=MediaType.MOVIE)


def test_best_match_empty_returns_none():
    assert best_match([], "Anything", 2020) is None


def test_best_match_tier1_exact_title_and_year():
    """Tier 1: exact normalized title + year."""
    matches = [_match("Coco", 2017, tmdb_id=1), _match("Coco", 2018, tmdb_id=2)]
    result = best_match(matches, "Coco", 2017)
    assert result is not None
    assert result.tmdb_id == 1


def test_best_match_tier1_accent_normalized():
    """Tier 1 with accent normalization: 'Pokémon' matches 'Pokemon'."""
    matches = [_match("Pokémon", 1997)]
    result = best_match(matches, "Pokemon", 1997)
    assert result is not None
    assert result.title == "Pokémon"


def test_best_match_tier2_exact_any_year():
    """Tier 2: exact title, year missing or different."""
    matches = [_match("Coco", 2017)]
    result = best_match(matches, "Coco", None)
    assert result is not None
    assert result.year == 2017


def test_best_match_tier2_year_mismatch_still_matches():
    """Tier 2 fires when guessed year doesn't equal any TMDB year."""
    matches = [_match("Coco", 2017)]
    result = best_match(matches, "Coco", 1999)
    assert result is not None


def test_best_match_tier3_substring_with_year():
    """Tier 3: guessed is a substring of TMDB title, same year."""
    matches = [_match("Pokémon: Destiny Deoxys", 2004)]
    result = best_match(matches, "Pokemon", 2004)
    assert result is not None


def test_best_match_tier3_tmdb_substring_of_guessed():
    """Tier 3 also matches when TMDB title is a substring of the guessed title."""
    matches = [_match("Coco", 2017)]
    result = best_match(matches, "Coco the Movie", 2017)
    assert result is not None


def test_best_match_tier4_first_result_substring_any_year():
    """Tier 4: only first result, year-agnostic substring match."""
    matches = [
        _match("Avatar: The Last Airbender", 2005, tmdb_id=10),
        _match("Some Other Show", 1999, tmdb_id=11),
    ]
    result = best_match(matches, "Avatar", None)
    assert result is not None
    assert result.tmdb_id == 10


def test_best_match_returns_none_when_nothing_fits():
    """No tier matches → None (caller falls back to interactive prompt)."""
    matches = [_match("Completely Different Show", 1999)]
    result = best_match(matches, "Avatar", 2009)
    assert result is None


def test_best_match_prefers_year_match_over_arbitrary():
    """When two TMDB results share the title, prefer the year match."""
    matches = [
        _match("Coco", 1995, tmdb_id=1),
        _match("Coco", 2017, tmdb_id=2),
    ]
    result = best_match(matches, "Coco", 2017)
    assert result is not None
    assert result.tmdb_id == 2


# ---------------------------------------------------------------------------
# TmdbClient — search_movie / search_tv / get_season_episodes
# Mocked via httpx.MockTransport so no network calls are made.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client(monkeypatch):
    """Patch httpx.Client inside jellyfiler.tmdb so the real _get runs but never hits network.

    Yields a setter — call it with a handler(request) -> httpx.Response to register
    the mock for a specific test.
    """
    real_client_class = httpx.Client
    handlers: dict[str, object] = {"current": None}

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client_class(transport=httpx.MockTransport(handlers["current"]), **kwargs)

    monkeypatch.setattr("jellyfiler.tmdb.httpx.Client", factory)

    def set_handler(handler):
        handlers["current"] = handler
        return TmdbClient(api_key="test-key")

    return set_handler


def test_search_movie_parses_results(mock_client):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": 1, "title": "Coco", "release_date": "2017-10-27"},
                    {"id": 2, "title": "Coco", "release_date": "1995-01-01"},
                ]
            },
        )

    client = mock_client(handler)
    results = client.search_movie("Coco", 2017)
    assert len(results) == 2
    assert results[0].tmdb_id == 1
    assert results[0].title == "Coco"
    assert results[0].year == 2017
    assert results[0].media_type == MediaType.MOVIE


def test_search_movie_handles_missing_release_date(mock_client):
    def handler(request):
        return httpx.Response(
            200, json={"results": [{"id": 9, "title": "Unreleased", "release_date": ""}]}
        )

    client = mock_client(handler)
    results = client.search_movie("Unreleased", None)
    assert results[0].year is None


def test_search_tv_parses_results(mock_client):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": 100, "name": "Futurama", "first_air_date": "1999-03-28"},
                ]
            },
        )

    client = mock_client(handler)
    results = client.search_tv("Futurama", None)
    assert len(results) == 1
    assert results[0].tmdb_id == 100
    assert results[0].title == "Futurama"
    assert results[0].year == 1999
    assert results[0].media_type == MediaType.EPISODE


def test_search_tv_with_year_filter_passes_first_air_date_year(mock_client):
    """When year is given, search_tv adds first_air_date_year to the params (line 56)."""
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"results": []})

    client = mock_client(handler)
    client.search_tv("Futurama", year=1999)
    assert "first_air_date_year=1999" in captured["url"]


def test_search_tv_empty_results(mock_client):
    def handler(request):
        return httpx.Response(200, json={"results": []})

    client = mock_client(handler)
    assert client.search_tv("nonexistent show", None) == []


def test_get_season_episodes_parses_episode_list(mock_client):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "episodes": [
                    {"episode_number": 1, "name": "Pilot"},
                    {"episode_number": 2, "name": "Second"},
                    {"episode_number": None, "name": "skip me"},  # filtered out
                ]
            },
        )

    client = mock_client(handler)
    eps = client.get_season_episodes(show_id=42, season=1)
    assert eps == [(1, "Pilot"), (2, "Second")]


# ---------------------------------------------------------------------------
# Rate limiting + 429 retry
# ---------------------------------------------------------------------------


def test_rate_limit_throttles_to_target_rps(mock_client, monkeypatch):
    """Two back-to-back requests should sleep at least 1/rps seconds in between."""
    sleeps: list[float] = []
    monkeypatch.setattr("jellyfiler.tmdb.time.sleep", lambda s: sleeps.append(s))
    # Pin monotonic so the first request "happened" right now and the second
    # immediately after — the rate limiter should sleep before the second one.
    fake_now = [100.0]

    def fake_monotonic():
        return fake_now[0]

    monkeypatch.setattr("jellyfiler.tmdb.time.monotonic", fake_monotonic)

    def handler(request):
        return httpx.Response(200, json={"results": []})

    client = mock_client(handler)
    client.rps = 10  # 100ms minimum interval

    # First request: _last_request_at is 0, so no sleep
    client.search_movie("a", None)
    # Advance clock by only 50ms — second request must sleep ~50ms
    fake_now[0] += 0.05
    client.search_movie("b", None)

    assert any(s > 0 for s in sleeps), f"expected at least one positive sleep, got {sleeps}"


def test_rate_limit_zero_rps_disables_throttling(mock_client, monkeypatch):
    """rps=0 means "go as fast as you like"."""
    sleeps: list[float] = []
    monkeypatch.setattr("jellyfiler.tmdb.time.sleep", lambda s: sleeps.append(s))

    def handler(request):
        return httpx.Response(200, json={"results": []})

    client = mock_client(handler)
    client.rps = 0
    for _ in range(3):
        client.search_movie("a", None)
    assert sleeps == []


def test_429_response_triggers_retry_with_retry_after(mock_client, monkeypatch):
    """When TMDB returns 429, the client honours Retry-After and tries once more."""
    sleeps: list[float] = []
    monkeypatch.setattr("jellyfiler.tmdb.time.sleep", lambda s: sleeps.append(s))

    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return httpx.Response(200, json={"results": []})

    client = mock_client(handler)
    client.rps = 0  # disable proactive throttling so we only see the retry sleep
    client.search_movie("a", None)
    assert call_count["n"] == 2  # got rate-limited then retried
    assert 2.0 in sleeps  # honoured the 2-second Retry-After


def test_429_with_invalid_retry_after_falls_back_to_one_second(mock_client, monkeypatch):
    """Garbage in the Retry-After header → default to 1 second."""
    sleeps: list[float] = []
    monkeypatch.setattr("jellyfiler.tmdb.time.sleep", lambda s: sleeps.append(s))

    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "abc"}, json={})
        return httpx.Response(200, json={"results": []})

    client = mock_client(handler)
    client.rps = 0
    client.search_movie("a", None)
    assert 1.0 in sleeps


def test_429_with_no_retry_after_header_uses_default(mock_client, monkeypatch):
    """Missing Retry-After → default 1s sleep."""
    sleeps: list[float] = []
    monkeypatch.setattr("jellyfiler.tmdb.time.sleep", lambda s: sleeps.append(s))

    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"results": []})

    client = mock_client(handler)
    client.rps = 0
    client.search_movie("a", None)
    assert 1.0 in sleeps


def test_429_only_retries_once_then_raises(mock_client):
    """Persistent 429 → second 429 raises; we don't retry forever."""
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"}, json={})

    client = mock_client(handler)
    client.rps = 0
    with pytest.raises(httpx.HTTPStatusError):
        client.search_movie("a", None)
    # Initial + 1 retry = 2 calls total
    assert call_count["n"] == 2


def test_search_movie_raises_on_http_error(mock_client):
    def handler(request):
        return httpx.Response(401, json={"status_message": "invalid api key"})

    client = mock_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.search_movie("anything", None)
