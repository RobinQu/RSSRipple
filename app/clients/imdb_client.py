"""IMDB client for movie/TV series metadata lookup using Cinemagoer.

Cinemagoer (formerly IMDbPY) scrapes IMDB for metadata.
No API key required — it parses IMDB pages directly.
See: https://cinemagoer.github.io/
"""

import asyncio
import logging
from functools import partial

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class IMDBSearchResult(BaseModel):
    """A single search result from IMDB."""
    imdb_id: str  # e.g. "tt1234567"
    title: str
    original_title: str
    kind: str  # "movie", "tv series", "tv mini series", etc.
    year: int | None = None


class IMDBTitleDetail(BaseModel):
    """Detailed title info from IMDB."""
    imdb_id: str
    title: str
    original_title: str
    kind: str
    year: int | None = None
    genres: list[str] = []
    plot: str | None = None
    rating: float | None = None
    number_of_episodes: int | None = None
    number_of_seasons: int | None = None


class IMDBClient:
    """Async wrapper around Cinemagoer for IMDB lookups.

    Cinemagoer is a synchronous library that scrapes IMDB.
    All calls are wrapped in asyncio.to_thread to avoid blocking.
    """

    def __init__(self):
        self._ia = None

    def _get_ia(self):
        """Lazy-init the Cinemagoer instance."""
        if self._ia is None:
            try:
                from cinemagoer import IMDb
                self._ia = IMDb()
            except ImportError:
                logger.error("cinemagoer not installed")
                return None
        return self._ia

    async def search(self, query: str, year: int | None = None) -> list[IMDBSearchResult]:
        """Search IMDB for movies and TV series.

        Args:
            query: Search query (title in any language).
            year: Optional year filter (applied post-search).

        Returns:
            List of IMDBSearchResult.
        """
        ia = self._get_ia()
        if ia is None:
            return []

        try:
            results = await asyncio.to_thread(partial(ia.search_title, query))
        except Exception as e:
            logger.error("IMDB search failed: %s", e)
            return []

        search_results = []
        for item in results[:10]:
            kind = item.get("kind", "").lower().replace(" ", " ")
            # Normalize kind to simple types
            if kind in ("movie", "tv series", "tv mini series", "tv movie"):
                item_year = item.get("year")
                if year and item_year and item_year != year:
                    continue
                search_results.append(IMDBSearchResult(
                    imdb_id=item.movieID,
                    title=item.get("title", ""),
                    original_title=item.get("original title", item.get("title", "")),
                    kind=kind,
                    year=item_year,
                ))
        return search_results

    async def get_title(self, imdb_id: str) -> IMDBTitleDetail | None:
        """Get detailed info for a specific IMDB title.

        Args:
            imdb_id: IMDB ID (e.g., "1234567" or "tt1234567").

        Returns:
            IMDBTitleDetail or None.
        """
        ia = self._get_ia()
        if ia is None:
            return None

        # Strip "tt" prefix if present
        clean_id = imdb_id.lstrip("tt")

        try:
            movie = await asyncio.to_thread(partial(ia.get_movie, clean_id))
        except Exception as e:
            logger.error("IMDB get_title failed for %s: %s", imdb_id, e)
            return None

        kind = movie.get("kind", "").lower().replace(" ", " ")

        return IMDBTitleDetail(
            imdb_id=clean_id,
            title=movie.get("title", ""),
            original_title=movie.get("original title", movie.get("title", "")),
            kind=kind,
            year=movie.get("year"),
            genres=movie.get("genres", []),
            plot=movie.get("plot outline") or (
                movie.get("plot", [""])[0] if movie.get("plot") else None
            ),
            rating=movie.get("rating"),
            number_of_episodes=movie.get("number of episodes"),
            number_of_seasons=movie.get("number of seasons"),
        )
