"""Unified Metadata Repository API — poster wall for both TVSeries and Movie."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.responses import JSONResponse

from app.database import get_db
from app.models.series import TVSeries
from app.models.movie import Movie
from app.schemas.common import paginated_response

router = APIRouter()


def _year_from_date(val: object) -> int | None:
    """Extract year from a date-like value (str or date)."""
    if val is None:
        return None
    s = str(val)
    if len(s) >= 4 and s[:4].isdigit():
        return int(s[:4])
    return None


def _normalize_series(s: TVSeries) -> dict:
    return {
        "id": s.id,
        "content_type": "tv",
        "title_cn": s.title_cn,
        "title_en": s.title_en,
        "original_title": s.original_title,
        "poster_url": s.poster_url,
        "rating": s.rating,
        "status": s.status,
        "year": _year_from_date(s.start_date),
        "genre": s.genre or [],
        "episodes": s.number_of_episodes,
        "seasons": s.number_of_seasons,
        "created_at": s.created_at.isoformat() + "Z" if s.created_at else None,
        "updated_at": s.updated_at.isoformat() + "Z" if s.updated_at else None,
    }


def _normalize_movie(m: Movie) -> dict:
    return {
        "id": m.id,
        "content_type": "movie",
        "title_cn": m.title_cn,
        "title_en": m.title_en,
        "original_title": m.original_title,
        "poster_url": m.poster_url,
        "rating": m.rating,
        "status": m.status,
        "year": _year_from_date(m.release_date),
        "genre": m.genre or [],
        "episodes": None,
        "seasons": None,
        "created_at": m.created_at.isoformat() + "Z" if m.created_at else None,
        "updated_at": m.updated_at.isoformat() + "Z" if m.updated_at else None,
    }


@router.get("/works")
async def list_works(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="Title fuzzy search"),
    content_type: str = Query("all", description="Filter: all, tv, movie"),
    db: AsyncSession = Depends(get_db),
):
    """Unified poster wall combining TVSeries and Movie in one list.

    Returns items sorted by ``created_at`` descending, with a ``content_type``
    discriminator field ("tv" or "movie").
    """
    works: list[dict] = []

    # Fetch from both tables
    if content_type in ("all", "tv"):
        series_q = select(TVSeries)
        if search:
            pattern = f"%{search}%"
            series_q = series_q.where(
                or_(
                    TVSeries.title_cn.ilike(pattern),
                    TVSeries.title_en.ilike(pattern),
                    TVSeries.original_title.ilike(pattern),
                )
            )
        result = await db.execute(series_q.order_by(TVSeries.created_at.desc()))
        for s in result.scalars().all():
            works.append(_normalize_series(s))

    if content_type in ("all", "movie"):
        movie_q = select(Movie)
        if search:
            pattern = f"%{search}%"
            movie_q = movie_q.where(
                or_(
                    Movie.title_cn.ilike(pattern),
                    Movie.title_en.ilike(pattern),
                    Movie.original_title.ilike(pattern),
                )
            )
        result = await db.execute(movie_q.order_by(Movie.created_at.desc()))
        for m in result.scalars().all():
            works.append(_normalize_movie(m))

    # Sort merged results by created_at descending
    works.sort(key=lambda w: w["created_at"] or "", reverse=True)

    total = len(works)
    offset = (page - 1) * page_size
    paged = works[offset:offset + page_size]

    return paginated_response(paged, total=total, page=page, page_size=page_size)
