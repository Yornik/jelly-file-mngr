"""AniList GraphQL API client — fallback for anime titles not found on TMDB.

AniList is free, requires no API key, and has comprehensive anime coverage
including correct titles, season structure, and episode counts.
"""

import re

import httpx

from jellyfiler.models import MediaType, TmdbMatch

ANILIST_URL = "https://graphql.anilist.co"
_REQUEST_TIMEOUT = 10

_SEARCH_QUERY = """
query ($search: String) {
  Page(page: 1, perPage: 10) {
    media(search: $search, type: ANIME) {
      id
      title {
        romaji
        english
        native
      }
      startDate {
        year
      }
      episodes
      season
      seasonYear
    }
  }
}
"""

# Patterns that suggest a file is anime
_ANIME_HINTS = re.compile(
    r"(\[[^\]]+\])"  # [SubGroup] prefix
    r"|[\u3000-\u9fff]"  # CJK characters
    r"|\b(BD|BDRip|BDRemux|BDMV)\b"  # BD source common in anime
    r"|\b(OVA|ONA|OAD)\b",  # anime-specific release types
    re.IGNORECASE,
)


def looks_like_anime(filename: str) -> bool:
    """Heuristic check — true if the filename has anime-release patterns."""
    return bool(_ANIME_HINTS.search(filename))


def search_anime(title: str) -> list[TmdbMatch]:
    """Search AniList for anime by title. Returns results as TmdbMatch objects.

    Uses the AniList ID prefixed with 'al-' as the tmdb_id so it can be
    distinguished from real TMDB IDs in the cache and plan output.
    """
    with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
        response = client.post(
            ANILIST_URL,
            json={"query": _SEARCH_QUERY, "variables": {"search": title}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    response.raise_for_status()
    data = response.json()

    media_list = data.get("data", {}).get("Page", {}).get("media", [])
    results = []
    for m in media_list:
        # Prefer English title, fall back to romaji
        display_title = (
            (m.get("title") or {}).get("english") or (m.get("title") or {}).get("romaji") or ""
        )
        if not display_title:
            continue
        year = (m.get("startDate") or {}).get("year") or (m.get("seasonYear"))
        results.append(
            TmdbMatch(
                tmdb_id=int(m["id"]),
                title=display_title,
                year=int(year) if year else None,
                media_type=MediaType.EPISODE,
            )
        )
    return results
