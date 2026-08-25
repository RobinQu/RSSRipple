"""Unified Metadata Repository API — poster wall for both TVSeries and Movie."""


from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.audio_work import AudioWork
from app.models.movie import Movie
from app.models.series import TVSeries
from app.schemas.common import paginated_response, success_response
from app.services.metadata_agent import (
    SUPPORTED_METADATA_SOURCES,
    get_metadata_source_catalog,
    is_metadata_source_available,
)
from app.services.metadata_service import refresh_work_metadata

router = APIRouter()


# ---------------------------------------------------------------------------
# Metadata refresh catalog + actions
#
# There is no global default source anymore: every manual refresh names its
# source explicitly and the per-channel periodic refresh uses the channel's
# own ``metadata_source``. All of them share the single
# ``refresh_work_metadata`` pipeline underneath.
# ---------------------------------------------------------------------------


def _resolve_explicit_source(source: str | None) -> str:
    """Validate a caller-supplied metadata source name."""
    v = (source or "").strip().lower()
    if not v:
        raise HTTPException(status_code=400, detail="metadata source is required")
    if v not in SUPPORTED_METADATA_SOURCES or not is_metadata_source_available(v):
        raise HTTPException(status_code=400, detail="metadata source is not available")
    return v


class RefreshItem(BaseModel):
    id: str
    content_type: str  # "tv" | "movie"


class RefreshMetadataRequest(RefreshItem):
    # Required: there is no global default source anymore — the refresh
    # dialog's picker always names one explicitly.
    source: str
    # Explicit opt-in to overwrite fields the user edited manually through the
    # work detail edit form. Defaults to False: automatic scans never clobber
    # manual edits unless the user ticks "覆盖所有人工编辑字段" in the dialog.
    override_manual_edits: bool = False


class BatchRefreshMetadataRequest(BaseModel):
    items: list[RefreshItem]
    source: str


@router.get("/works/metadata-config")
async def get_metadata_config(db: AsyncSession = Depends(get_db)):
    """Return the external metadata source catalog (for refresh pickers)."""
    return success_response({
        "sources": get_metadata_source_catalog(),
    })


@router.post("/works/refresh-metadata")
async def refresh_single_metadata(
    body: RefreshMetadataRequest, db: AsyncSession = Depends(get_db)
):
    """Refresh a single work's missing metadata fields from the given source."""
    source = _resolve_explicit_source(body.source)
    result = await refresh_work_metadata(
        db, body.id, body.content_type, source, override_manual_edits=body.override_manual_edits
    )
    return success_response(result)


@router.post("/works/batch-refresh-metadata")
async def batch_refresh_metadata(
    body: BatchRefreshMetadataRequest, db: AsyncSession = Depends(get_db)
):
    """Enqueue a background job to refresh metadata for many works at once.

    Each work is processed sequentially against the same source. Returns the
    job descriptor so the client can poll status.
    """
    if not body.items:
        return success_response({"job": None, "count": 0, "source": None})
    source = _resolve_explicit_source(body.source)
    from app.services.task_queue import task_queue

    job = await task_queue.enqueue(
        "refresh_works_metadata",
        f"refresh_works:{uuid4().hex}",
        {
            "items": [item.model_dump() for item in body.items],
            "source": source,
        },
    )
    return success_response({"job": job, "count": len(body.items), "source": source})



def _year_from_date(val: object) -> int | None:
    """Extract year from a date-like value (str or date)."""
    if val is None:
        return None
    s = str(val)
    if len(s) >= 4 and s[:4].isdigit():
        return int(s[:4])
    return None


def _collection_fields(w: TVSeries | Movie) -> dict:
    """Collection display fields: name is title_cn or title_en, None when ungrouped."""
    c = w.collection
    return {
        "collection_id": w.collection_id,
        "collection_name": (c.title_cn or c.title_en) if c else None,
    }


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
        "is_anime": s.is_anime,
        "episodes": s.number_of_episodes,
        "seasons": s.number_of_seasons,
        "number_of_episodes": s.number_of_episodes,
        "number_of_seasons": s.number_of_seasons,
        "release_date": None,
        "runtime": None,
        "created_at": s.created_at.isoformat() + "Z" if s.created_at else None,
        "updated_at": s.updated_at.isoformat() + "Z" if s.updated_at else None,
        **_collection_fields(s),
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
        "is_anime": m.is_anime,
        "episodes": None,
        "seasons": None,
        "number_of_episodes": None,
        "number_of_seasons": None,
        "release_date": str(m.release_date) if m.release_date else None,
        "runtime": m.runtime,
        "created_at": m.created_at.isoformat() + "Z" if m.created_at else None,
        "updated_at": m.updated_at.isoformat() + "Z" if m.updated_at else None,
        **_collection_fields(m),
    }


@router.get("/works")
async def list_works(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="Title fuzzy search"),
    content_type: str = Query(
        "all", description="Filter: all, tv, movie, audio, asmr, music, drama_cd, radio, other"
    ),
    collection_id: str | None = Query(
        None, description="Collection UUID, or 'none' for works without a collection"
    ),
    db: AsyncSession = Depends(get_db),
):
    """Unified poster wall combining TVSeries, Movie, and AudioWork in one list.

    Returns items sorted by ``created_at`` descending, with a ``content_type``
    discriminator field ("tv", "movie", or an audio sub-kind). When
    ``collection_id`` is present only series/movies match (audio excluded);
    the literal "none" selects works without a collection.
    """
    works: list[dict] = []
    audio_types = {"asmr", "music", "drama_cd", "radio", "other"}
    include_audio = (
        (content_type in ("all", "audio") or content_type in audio_types)
        and collection_id is None
    )

    # Fetch from both tables
    if content_type in ("all", "tv"):
        series_q = select(TVSeries).options(selectinload(TVSeries.collection))
        if collection_id == "none":
            series_q = series_q.where(TVSeries.collection_id.is_(None))
        elif collection_id is not None:
            series_q = series_q.where(TVSeries.collection_id == collection_id)
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
        movie_q = select(Movie).options(selectinload(Movie.collection))
        if collection_id == "none":
            movie_q = movie_q.where(Movie.collection_id.is_(None))
        elif collection_id is not None:
            movie_q = movie_q.where(Movie.collection_id == collection_id)
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

    if include_audio:
        audio_q = select(AudioWork)
        if content_type in audio_types:
            audio_q = audio_q.where(AudioWork.content_type == content_type)
        if search:
            pattern = f"%{search}%"
            audio_q = audio_q.where(
                or_(
                    AudioWork.title_cn.ilike(pattern),
                    AudioWork.title_en.ilike(pattern),
                    AudioWork.original_title.ilike(pattern),
                )
            )
        result = await db.execute(audio_q.order_by(AudioWork.created_at.desc()))
        for a in result.scalars().all():
            works.append(_normalize_audio_work(a))

    # Sort merged results by created_at descending
    works.sort(key=lambda w: w["created_at"] or "", reverse=True)

    total = len(works)
    offset = (page - 1) * page_size
    paged = works[offset:offset + page_size]

    return paginated_response(paged, total=total, page=page, page_size=page_size)


def _normalize_audio_work(a: AudioWork) -> dict:
    return {
        "id": a.id,
        "content_type": a.content_type or "other",
        "title_cn": a.title_cn,
        "title_en": a.title_en,
        "original_title": a.original_title,
        "poster_url": a.poster_url,
        "rating": a.rating,
        "status": a.status,
        "year": _year_from_date(a.release_date),
        "genre": a.genre or [],
        "episodes": None,
        "seasons": None,
        "number_of_episodes": None,
        "number_of_seasons": None,
        "release_date": str(a.release_date) if a.release_date else None,
        "runtime": a.runtime,
        "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
        "updated_at": a.updated_at.isoformat() + "Z" if a.updated_at else None,
    }
