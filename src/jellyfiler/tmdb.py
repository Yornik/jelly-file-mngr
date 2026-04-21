"""TMDB API client — search for movies and TV shows."""

from dataclasses import dataclass
from typing import Any

import httpx

from jellyfiler.models import MediaType, TmdbMatch

__all__ = ["TmdbClient", "TmdbMatch", "best_match"]

TMDB_BASE = "https://api.themoviedb.org/3"
_REQUEST_TIMEOUT = 10


@dataclass
class TmdbClient:
    api_key: str

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {"api_key": self.api_key, **params}
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            response = client.get(f"{TMDB_BASE}{endpoint}", params=params)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def search_movie(self, title: str, year: int | None = None) -> list[TmdbMatch]:
        params: dict[str, Any] = {"query": title, "include_adult": "false"}
        if year:
            params["year"] = year
        data = self._get("/search/movie", params)
        results: list[dict[str, Any]] = data.get("results", [])
        return [
            TmdbMatch(
                tmdb_id=r["id"],
                title=r["title"],
                year=int(r["release_date"][:4]) if r.get("release_date") else None,
                media_type=MediaType.MOVIE,
            )
            for r in results
        ]

    def get_season_episodes(self, show_id: int, season: int) -> list[tuple[int, str]]:
        """Return [(episode_number, episode_name), ...] for a season."""
        data = self._get(f"/tv/{show_id}/season/{season}", {})
        return [
            (int(ep["episode_number"]), ep.get("name", ""))
            for ep in data.get("episodes", [])
            if ep.get("episode_number") is not None
        ]

    def search_tv(self, title: str, year: int | None = None) -> list[TmdbMatch]:
        params: dict[str, Any] = {"query": title}
        if year:
            params["first_air_date_year"] = year
        data = self._get("/search/tv", params)
        results: list[dict[str, Any]] = data.get("results", [])
        return [
            TmdbMatch(
                tmdb_id=r["id"],
                title=r["name"],
                year=int(r["first_air_date"][:4]) if r.get("first_air_date") else None,
                media_type=MediaType.EPISODE,
            )
            for r in results
        ]


def _norm(s: str) -> str:
    """Lowercase and strip unicode accents (e.g. é→e) for fuzzy comparison."""
    import unicodedata

    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _norm_punct(s: str) -> str:
    """Normalize title punctuation so & / ! : - all compare equal.

    "Superman & Shazam - The Return of Black Adam"
    vs "Superman/Shazam!: The Return of Black Adam" → both become
    "superman shazam the return of black adam"
    """
    import re

    s = _norm(s)
    s = re.sub(r"[&/!:,\-_]+", " ", s)
    return " ".join(s.split())


def best_match(
    matches: list[TmdbMatch],
    guessed_title: str,
    guessed_year: int | None,
) -> TmdbMatch | None:
    """Return the best TMDB match or None if confidence is too low.

    Matching tiers (first hit wins):
    1. Exact title (accent-normalized) + year
    2. Exact title, any year
    3. Guessed title is a prefix/substring of a TMDB title, same year
    4. Guessed title is a prefix/substring of the top result, any year
    5. Punct-normalized exact match + year  (& vs / vs : vs - treated equal)
    6. Punct-normalized exact match, any year
    """
    if not matches:
        return None

    g = _norm(guessed_title)

    # 1. Exact + year
    for m in matches:
        if _norm(m.title) == g and m.year == guessed_year:
            return m

    # 2. Exact, any year
    for m in matches:
        if _norm(m.title) == g:
            return m

    # 3. Guessed title is contained in TMDB title (e.g. "Pokemon" in "Pokemon: Destiny Deoxys")
    #    — prefer the one where year also matches
    for m in matches:
        mt = _norm(m.title)
        if (g in mt or mt in g) and m.year == guessed_year:
            return m

    # 4. Same substring check, first result only, any year
    first = matches[0]
    ft = _norm(first.title)
    if g in ft or ft in g:
        return first

    # 5 & 6. Punct-normalized exact match — catches & vs / vs : vs - variations
    gp = _norm_punct(guessed_title)
    for m in matches:
        if _norm_punct(m.title) == gp and m.year == guessed_year:
            return m
    for m in matches:
        if _norm_punct(m.title) == gp:
            return m

    return None
