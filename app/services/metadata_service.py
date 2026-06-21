"""Metadata matching service for TVSeries and Movie entities.

Provides unified metadata lookup using TMDB and TVDB APIs.
Matching flow:
1. Local match: search TVSeries/Movie by title + aliases + fuzzy matching
2. External match: query TMDB/TVDB if agent has metadata_source configured
3. Fallback: caller creates PendingDecision for manual matching
"""

import logging
from datetime import date

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from thefuzz import fuzz

from app.models.series import TVSeries
from app.models.movie import Movie
from app.clients.tmdb_client import TMDBClient
from app.clients.tvdb_client import TVDBClient

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 70  # Minimum fuzzy match score (0-100)


async def match_series_by_title(
    db: AsyncSession,
    title: str,
) -> TVSeries | None:
    """Try to find a matching TVSeries in the local database.

    Searches by title_cn, title_en, and aliases with fuzzy matching.

    Args:
        db: Database session.
        title: Title to search for.

    Returns:
        Matched TVSeries or None.
    """
    # Exact match first
    result = await db.execute(
        select(TVSeries).where(
            or_(TVSeries.title_cn == title, TVSeries.title_en == title)
        )
    )
    series = result.scalar_one_or_none()
    if series:
        return series

    # Fuzzy match across all series
    all_result = await db.execute(select(TVSeries))
    all_series = all_result.scalars().all()

    best_match = None
    best_score = 0

    for s in all_series:
        candidates = [s.title_cn, s.title_en]
        if s.aliases:
            candidates.extend(s.aliases)
        candidates = [c for c in candidates if c]

        for candidate in candidates:
            score = fuzz.ratio(title.lower(), candidate.lower())
            if score > best_score and score >= FUZZY_THRESHOLD:
                best_score = score
                best_match = s

    return best_match


async def match_movie_by_title(
    db: AsyncSession,
    title: str,
) -> Movie | None:
    """Try to find a matching Movie in the local database.

    Args:
        db: Database session.
        title: Title to search for.

    Returns:
        Matched Movie or None.
    """
    result = await db.execute(
        select(Movie).where(
            or_(Movie.title_cn == title, Movie.title_en == title)
        )
    )
    movie = result.scalar_one_or_none()
    if movie:
        return movie

    all_result = await db.execute(select(Movie))
    all_movies = all_result.scalars().all()

    best_match = None
    best_score = 0

    for m in all_movies:
        candidates = [m.title_cn, m.title_en]
        if m.aliases:
            candidates.extend(m.aliases)
        candidates = [c for c in candidates if c]

        for candidate in candidates:
            score = fuzz.ratio(title.lower(), candidate.lower())
            if score > best_score and score >= FUZZY_THRESHOLD:
                best_score = score
                best_match = m

    return best_match


async def search_external_metadata(
    title: str,
    metadata_source: str,
    content_type: str = "anime",
    year: int | None = None,
) -> dict | None:
    """Search external metadata APIs (TMDB/TVDB) for a title.

    Args:
        title: Title to search.
        metadata_source: "tmdb" or "tvdb".
        content_type: "anime", "tv", "movie", or "mixed".
        year: Optional year filter.

    Returns:
        Dict with external metadata or None if no match.
    """
    if metadata_source == "tmdb":
        client = TMDBClient()
        if not client.is_configured:
            logger.warning("TMDB API key not configured")
            return None
        results = await client.search(title, year=year)
        if not results:
            return None
        best = results[0]
        return {
            "external_id": str(best.id),
            "external_source": "tmdb",
            "title": best.title,
            "original_title": best.original_title,
            "overview": best.overview,
            "media_type": best.media_type,
            "release_date": best.release_date or best.first_air_date,
        }
    elif metadata_source == "tvdb":
        client = TVDBClient()
        if not client.is_configured:
            logger.warning("TVDB API key not configured")
            return None
        search_type = "movie" if content_type == "movie" else "series"
        results = await client.search(title, search_type=search_type)
        if not results:
            return None
        best = results[0]
        return {
            "external_id": str(best.tvdb_id),
            "external_source": "tvdb",
            "title": best.name,
            "overview": best.overview,
            "media_type": best.type,
            "release_date": best.first_air_time or best.year,
        }

    return None


async def create_or_update_series_from_external(
    db: AsyncSession,
    external_data: dict,
    title_cn: str | None = None,
    title_en: str | None = None,
) -> TVSeries:
    """Create or update a TVSeries from external metadata.

    If a series with the same external_id exists, updates it (adds aliases).
    Otherwise creates a new one. This enables the auto-fix behavior:
    repeated verification failures resolve automatically after one
    successful manual match.

    Args:
        db: Database session.
        external_data: Dict from search_external_metadata.
        title_cn: Chinese title (from RSS parsing).
        title_en: English title (from RSS parsing).

    Returns:
        Created or updated TVSeries.
    """
    result = await db.execute(
        select(TVSeries).where(
            TVSeries.external_id == external_data["external_id"],
            TVSeries.external_source == external_data["external_source"],
        )
    )
    series = result.scalar_one_or_none()

    if series:
        # Auto-fix: add new title variants as aliases
        existing = set([series.title_cn, series.title_en] + (series.aliases or []))
        new_aliases = list(series.aliases or [])
        if title_cn and title_cn not in existing:
            new_aliases.append(title_cn)
            existing.add(title_cn)
        if title_en and title_en not in existing:
            new_aliases.append(title_en)
            existing.add(title_en)
        series.aliases = new_aliases if new_aliases else None
    else:
        aliases = []
        if title_cn:
            aliases.append(title_cn)
        if title_en:
            aliases.append(title_en)

        series = TVSeries(
            title_cn=title_cn,
            title_en=title_en or external_data.get("title"),
            aliases=aliases if aliases else None,
            external_id=external_data["external_id"],
            external_source=external_data["external_source"],
            description=external_data.get("overview"),
            content_type="tv",
        )
        if external_data.get("release_date"):
            try:
                series.start_date = date.fromisoformat(str(external_data["release_date"]))
            except (ValueError, TypeError):
                pass
        db.add(series)

    await db.flush()
    return series


async def create_or_update_movie_from_external(
    db: AsyncSession,
    external_data: dict,
    title_cn: str | None = None,
    title_en: str | None = None,
) -> Movie:
    """Create or update a Movie from external metadata.

    Same auto-fix logic as series: adds new title variants as aliases
    on existing matches.
    """
    result = await db.execute(
        select(Movie).where(
            Movie.external_id == external_data["external_id"],
            Movie.external_source == external_data["external_source"],
        )
    )
    movie = result.scalar_one_or_none()

    if movie:
        existing = set([movie.title_cn, movie.title_en] + (movie.aliases or []))
        new_aliases = list(movie.aliases or [])
        if title_cn and title_cn not in existing:
            new_aliases.append(title_cn)
            existing.add(title_cn)
        if title_en and title_en not in existing:
            new_aliases.append(title_en)
            existing.add(title_en)
        movie.aliases = new_aliases if new_aliases else None
    else:
        aliases = []
        if title_cn:
            aliases.append(title_cn)
        if title_en:
            aliases.append(title_en)

        movie = Movie(
            title_cn=title_cn,
            title_en=title_en or external_data.get("title"),
            aliases=aliases if aliases else None,
            external_id=external_data["external_id"],
            external_source=external_data["external_source"],
            description=external_data.get("overview"),
            content_type="movie",
        )
        if external_data.get("release_date"):
            try:
                movie.release_date = date.fromisoformat(str(external_data["release_date"]))
            except (ValueError, TypeError):
                pass
        db.add(movie)

    await db.flush()
    return movie
