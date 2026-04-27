"""Tests for AniList anime detection heuristic and search client."""

import httpx
import pytest

from jellyfiler.anilist import looks_like_anime, search_anime
from jellyfiler.models import MediaType


def test_detects_subgroup_prefix():
    assert looks_like_anime("[HorribleSubs] Steins;Gate - 01 [720p].mkv")


def test_detects_erai_raws():
    assert looks_like_anime("[Erai-raws] Vinland Saga S2 - 24 [1080p].mkv")


def test_detects_bd_source():
    assert looks_like_anime("Fullmetal.Alchemist.Brotherhood.BDRip.1080p.mkv")


def test_detects_ova():
    assert looks_like_anime("Evangelion.OVA.mkv")


def test_normal_show_not_anime():
    assert not looks_like_anime("Breaking.Bad.S01E01.1080p.BluRay.x264.mkv")


def test_normal_movie_not_anime():
    assert not looks_like_anime("Blade.Runner.2049.2017.2160p.UHD.BluRay.mkv")


def test_futurama_not_anime():
    assert not looks_like_anime("Futurama.S12E03.1080p.x265-ELiTE.mkv")


# ---------------------------------------------------------------------------
# search_anime — HTTP-mocked GraphQL responses
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_anilist(monkeypatch):
    """Patch httpx.Client inside jellyfiler.anilist with a MockTransport-backed client."""
    real_client_class = httpx.Client
    handlers: dict[str, object] = {"current": None}

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client_class(transport=httpx.MockTransport(handlers["current"]), **kwargs)

    monkeypatch.setattr("jellyfiler.anilist.httpx.Client", factory)

    def set_handler(handler):
        handlers["current"] = handler

    return set_handler


def test_search_anime_prefers_english_title(mock_anilist):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "Page": {
                        "media": [
                            {
                                "id": 11061,
                                "title": {
                                    "romaji": "Hunter x Hunter (2011)",
                                    "english": "Hunter x Hunter",
                                    "native": "ハンター×ハンター",
                                },
                                "startDate": {"year": 2011},
                                "season": "AUTUMN",
                                "seasonYear": 2011,
                            }
                        ]
                    }
                }
            },
        )

    mock_anilist(handler)
    results = search_anime("hunter")
    assert len(results) == 1
    assert results[0].tmdb_id == 11061
    assert results[0].title == "Hunter x Hunter"  # English chosen over romaji
    assert results[0].year == 2011
    assert results[0].media_type == MediaType.EPISODE


def test_search_anime_falls_back_to_romaji(mock_anilist):
    """When English title is missing, romaji is used."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "Page": {
                        "media": [
                            {
                                "id": 1,
                                "title": {"romaji": "Steins;Gate", "english": None},
                                "startDate": {"year": 2011},
                                "seasonYear": None,
                            }
                        ]
                    }
                }
            },
        )

    mock_anilist(handler)
    results = search_anime("steins gate")
    assert results[0].title == "Steins;Gate"


def test_search_anime_uses_season_year_when_start_year_missing(mock_anilist):
    """startDate.year missing → fall back to seasonYear."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "Page": {
                        "media": [
                            {
                                "id": 99,
                                "title": {"romaji": "X", "english": "Show X"},
                                "startDate": {"year": None},
                                "seasonYear": 2018,
                            }
                        ]
                    }
                }
            },
        )

    mock_anilist(handler)
    results = search_anime("show x")
    assert results[0].year == 2018


def test_search_anime_skips_entries_with_no_title(mock_anilist):
    """An entry with neither English nor romaji title is filtered out."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "Page": {
                        "media": [
                            {
                                "id": 1,
                                "title": {"english": None, "romaji": None},
                                "startDate": {"year": 2020},
                                "seasonYear": 2020,
                            },
                            {
                                "id": 2,
                                "title": {"english": "Real Show", "romaji": "Riaru Shou"},
                                "startDate": {"year": 2021},
                                "seasonYear": 2021,
                            },
                        ]
                    }
                }
            },
        )

    mock_anilist(handler)
    results = search_anime("anything")
    assert len(results) == 1
    assert results[0].tmdb_id == 2


def test_search_anime_empty_results(mock_anilist):
    def handler(request):
        return httpx.Response(200, json={"data": {"Page": {"media": []}}})

    mock_anilist(handler)
    assert search_anime("nothing matches") == []


def test_search_anime_handles_missing_year_fields(mock_anilist):
    """Both startDate and seasonYear absent → year is None."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "Page": {
                        "media": [
                            {
                                "id": 7,
                                "title": {"english": "Yearless", "romaji": "Yearless"},
                                "startDate": {},
                                "seasonYear": None,
                            }
                        ]
                    }
                }
            },
        )

    mock_anilist(handler)
    results = search_anime("yearless")
    assert results[0].year is None


def test_search_anime_raises_on_http_error(mock_anilist):
    def handler(request):
        return httpx.Response(500, json={"errors": [{"message": "boom"}]})

    mock_anilist(handler)
    with pytest.raises(httpx.HTTPStatusError):
        search_anime("anything")
