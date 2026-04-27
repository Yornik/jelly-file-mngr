"""TMDB API client — search for movies and TV shows.

Rate limiting
-------------
TMDB's documented limit on free API keys is 50 requests per second per key.
The client throttles to a configurable rate (default 40 RPS — comfortably
below the cap so we don't get clipped by clock drift or bursts) and also
honours a 429 ``Retry-After`` response header by sleeping and retrying once.
"""

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from jellyfiler.models import MediaType, TmdbMatch

__all__ = ["TmdbClient", "TmdbMatch", "best_match"]

TMDB_BASE = "https://api.themoviedb.org/3"
_REQUEST_TIMEOUT = 10

# Default rate limit: 40 requests per second, well under TMDB's documented 50 RPS cap
# for free API keys. One extra request per second of headroom against burstiness.
_DEFAULT_RPS = 40.0


@dataclass
class TmdbClient:
    api_key: str
    # Max requests per second this client will issue. Set to 0 to disable throttling.
    rps: float = _DEFAULT_RPS
    # Internal: monotonic timestamp of the last completed request (or 0 if none yet).
    _last_request_at: float = field(default=0.0, init=False, repr=False)

    def _wait_for_rate_limit(self) -> None:
        """Sleep just enough to keep our outbound rate under ``self.rps``."""
        if self.rps <= 0:
            return
        min_interval = 1.0 / self.rps
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {"api_key": self.api_key, **params}
        url = f"{TMDB_BASE}{endpoint}"
        for attempt in range(2):  # at most one retry on 429
            self._wait_for_rate_limit()
            with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
                response = client.get(url, params=params)
            self._last_request_at = time.monotonic()
            if response.status_code == 429 and attempt == 0:
                # TMDB tells us how long to wait; default to 1s if header is missing.
                retry_after_header = response.headers.get("Retry-After", "1")
                try:
                    retry_after = float(retry_after_header)
                except ValueError:
                    retry_after = 1.0
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result
        # Unreachable: the loop either returns or raises before exiting.
        raise RuntimeError("TMDB request retry loop exited unexpectedly")

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

    return None
