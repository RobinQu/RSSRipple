"""TMDB API client for movie/TV series metadata lookup.

Uses The Movie Database API (https://www.themoviedb.org/documentation/api).
API key configured via settings.tmdb_api_key.
"""

import logging
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TMDB_BASE_URL = "https://api.themoviedb.org/3"


@dataclass
class TMDBSearchResult:
    """A single search result from TMDB."""
    id: int
    title: str
    original_title: str
    overview: str
    media_type: str  # "movie" or "tv"
    release_date: str | None = None
    first_air_date: str | None = None


@dataclass
class TMDBSeriesDetail:
    """Detailed TV series info from TMDB."""
    id: int
    name: str
    original_name: str
    overview: str
    genres: list[str]
    first_air_date: str | None
    number_of_seasons: int
    number_of_episodes: int


@dataclass
class TMDBMovieDetail:
    """Detailed movie info from TMDB."""
    id: int
    title: str
    original_title: str
    overview: str
    genres: list[str]
    release_date: str | None


class TMDBClient:
    """Async client for TMDB API v3."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.tmdb_api_key
        self.base_url = TMDB_BASE_URL

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, year: int | None = None) -> list[TMDBSearchResult]:
        """Search TMDB for movies and TV series (multi-search).

        Args:
            query: Search query (title in any language).
            year: Optional year filter.

        Returns:
            List of TMDBSearchResult.
        """
        if not self.is_configured:
            logger.warning("TMDB API key not configured")
            return []

        params: dict = {
            "api_key": self.api_key,
            "query": query,
            "include_adult": False,
            "language": "zh-CN",
        }
        if year:
            params["year"] = year

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/search/multi", params=params)
            if resp.status_code != 200:
                logger.error("TMDB search failed: %s %s", resp.status_code, resp.text)
                return []
            data = resp.json()

        results = []
        for item in data.get("results", []):
            media_type = item.get("media_type", "")
            if media_type not in ("movie", "tv"):
                continue
            results.append(TMDBSearchResult(
                id=item["id"],
                title=item.get("title") or item.get("name", ""),
                original_title=item.get("original_title") or item.get("original_name", ""),
                overview=item.get("overview", ""),
                media_type=media_type,
                release_date=item.get("release_date"),
                first_air_date=item.get("first_air_date"),
            ))
        return results

    async def get_series(self, series_id: int) -> TMDBSeriesDetail | None:
        """Get detailed TV series info from TMDB."""
        if not self.is_configured:
            return None

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/tv/{series_id}",
                params={"api_key": self.api_key, "language": "zh-CN"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()

        return TMDBSeriesDetail(
            id=data["id"],
            name=data.get("name", ""),
            original_name=data.get("original_name", ""),
            overview=data.get("overview", ""),
            genres=[g["name"] for g in data.get("genres", [])],
            first_air_date=data.get("first_air_date"),
            number_of_seasons=data.get("number_of_seasons", 0),
            number_of_episodes=data.get("number_of_episodes", 0),
        )

    async def get_movie(self, movie_id: int) -> TMDBMovieDetail | None:
        """Get detailed movie info from TMDB."""
        if not self.is_configured:
            return None

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/movie/{movie_id}",
                params={"api_key": self.api_key, "language": "zh-CN"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()

        return TMDBMovieDetail(
            id=data["id"],
            title=data.get("title", ""),
            original_title=data.get("original_title", ""),
            overview=data.get("overview", ""),
            genres=[g["name"] for g in data.get("genres", [])],
            release_date=data.get("release_date"),
        )
