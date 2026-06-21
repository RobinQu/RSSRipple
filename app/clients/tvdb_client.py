"""TVDB API v4 client for TV series metadata lookup.

Uses TheTVDB API v4 (https://thetvdb.github.io/v4-api/).
API key configured via settings.tvdb_api_key.
"""

import logging
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TVDB_BASE_URL = "https://api4.thetvdb.com/v4"


@dataclass
class TVDBSearchResult:
    """A single search result from TVDB."""
    id: int
    name: str
    overview: str
    tvdb_id: int
    type: str  # "series" or "movie"
    year: str | None = None
    first_air_time: str | None = None


@dataclass
class TVDBSeriesDetail:
    """Detailed TV series info from TVDB."""
    id: int
    name: str
    overview: str
    genres: list[str]
    first_air_time: str | None
    status: str | None
    year: str | None


class TVDBClient:
    """Async client for TVDB API v4."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.tvdb_api_key
        self.base_url = TVDB_BASE_URL
        self._token: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def _get_token(self) -> str | None:
        """Authenticate and get a bearer token."""
        if self._token:
            return self._token

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/login",
                json={"apikey": self.api_key},
            )
            if resp.status_code != 200:
                logger.error("TVDB auth failed: %s", resp.status_code)
                return None
            data = resp.json()
            self._token = data.get("data", {}).get("token")
            return self._token

    async def search(self, query: str, search_type: str = "series") -> list[TVDBSearchResult]:
        """Search TVDB for series or movies.

        Args:
            query: Search query.
            search_type: "series" or "movie".

        Returns:
            List of TVDBSearchResult.
        """
        if not self.is_configured:
            logger.warning("TVDB API key not configured")
            return []

        token = await self._get_token()
        if not token:
            return []

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/search",
                params={"query": query, "type": search_type},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                logger.error("TVDB search failed: %s", resp.status_code)
                return []
            data = resp.json()

        results = []
        for item in data.get("data", []):
            results.append(TVDBSearchResult(
                id=item.get("id", 0),
                name=item.get("name", ""),
                overview=item.get("overview", ""),
                tvdb_id=item.get("tvdb_id", item.get("id", 0)),
                type=search_type,
                year=item.get("year"),
                first_air_time=item.get("first_air_time"),
            ))
        return results

    async def get_series(self, series_id: int) -> TVDBSeriesDetail | None:
        """Get detailed TV series info from TVDB."""
        if not self.is_configured:
            return None

        token = await self._get_token()
        if not token:
            return None

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.base_url}/series/{series_id}/extended",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json().get("data", {})

        return TVDBSeriesDetail(
            id=data.get("id", 0),
            name=data.get("name", ""),
            overview=data.get("overview", ""),
            genres=[g.get("name", "") for g in data.get("genres", [])],
            first_air_time=data.get("first_air_time"),
            status=(
                data.get("status", {}).get("name")
                if isinstance(data.get("status"), dict)
                else None
            ),
            year=data.get("year"),
        )
