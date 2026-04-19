"""TMDB API client — search for movies and TV shows."""

from dataclasses import dataclass

import httpx

from jellyfiler.models import MediaType, TmdbMatch

TMDB_BASE = "https://api.themoviedb.org/3"
_REQUEST_TIMEOUT = 10


@dataclass
class TmdbClient:
    api_key: str

    def _get(self, endpoint: str, params: dict) -> dict:
        params = {"api_key": self.api_key, **params}
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            response = client.get(f"{TMDB_BASE}{endpoint}", params=params)
        response.raise_for_status()
        return response.json()

    def search_movie(self, title: str, year: int | None = None) -> list[TmdbMatch]:
        params: dict = {"query": title, "include_adult": "false"}
        if year:
            params["year"] = year
        data = self._get("/search/movie", params)
        results = data.get("results", [])
        return [
            TmdbMatch(
                tmdb_id=r["id"],
                title=r["title"],
                year=int(r["release_date"][:4]) if r.get("release_date") else None,
                media_type=MediaType.MOVIE,
            )
            for r in results
        ]

    def search_tv(self, title: str, year: int | None = None) -> list[TmdbMatch]:
        params: dict = {"query": title}
        if year:
            params["first_air_date_year"] = year
        data = self._get("/search/tv", params)
        results = data.get("results", [])
        return [
            TmdbMatch(
                tmdb_id=r["id"],
                title=r["name"],
                year=int(r["first_air_date"][:4]) if r.get("first_air_date") else None,
                media_type=MediaType.EPISODE,
            )
            for r in results
        ]


def best_match(
    matches: list[TmdbMatch],
    guessed_title: str,
    guessed_year: int | None,
) -> TmdbMatch | None:
    """Return the best TMDB match or None if confidence is too low."""
    if not matches:
        return None

    guessed_lower = guessed_title.lower()

    # Prefer exact title + year match
    for m in matches:
        if m.title.lower() == guessed_lower and m.year == guessed_year:
            return m

    # Prefer exact title match regardless of year
    for m in matches:
        if m.title.lower() == guessed_lower:
            return m

    # Fall back to first result only if title is at least a substring match
    first = matches[0]
    if guessed_lower in first.title.lower() or first.title.lower() in guessed_lower:
        return first

    return None
