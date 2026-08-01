"""Work-title substring search for TVSeries, Movie, and AudioWork.

All indexed text and queries are passed through
``text_normalizer.normalize_title`` (NFKC + OpenCC t2s + lowercase) so that
Traditional/Simplified, half/full-width, and case variants all match.
Matching is done in Python over the (small) work tables; callers compute
``similarity_score`` on the returned candidates for precise ranking.

Historically this module maintained SQLite FTS5 trigram indexes; those were
dropped when the storage engine moved to Turso (no FTS5 support).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.text_normalizer import normalize_title

logger = logging.getLogger(__name__)


async def _search_entities_like(db: AsyncSession, model: Any, norm: str, limit: int) -> list[str]:
    """Normalized substring search over a work table.

    Scans the (small) work table and matches the normalized query against the
    normalized titles/aliases in Python.
    """
    ids: list[str] = []
    try:
        result = await db.execute(select(model))
        entities = result.scalars().all()
    except Exception as e:
        logger.warning("[fts] LIKE search failed: %s", e)
        return []
    for e in entities:
        haystack = " ".join(filter(None, [
            normalize_title(e.title_cn),
            normalize_title(e.title_en),
            normalize_title(e.original_title),
            " ".join(normalize_title(a) for a in (e.aliases or []) if a),
        ]))
        if norm in haystack:
            ids.append(e.id)
            if len(ids) >= limit:
                break
    return ids


async def search_series_fts(
    db: AsyncSession, query: str, limit: int = 30
) -> list[str]:
    """Search series by title. Returns a list of series entity IDs."""
    from app.models.series import TVSeries

    norm = normalize_title(query)
    if not norm:
        return []
    return await _search_entities_like(db, TVSeries, norm, limit)


async def search_movie_fts(
    db: AsyncSession, query: str, limit: int = 30
) -> list[str]:
    """Search movies by title. Returns a list of movie entity IDs."""
    from app.models.movie import Movie

    norm = normalize_title(query)
    if not norm:
        return []
    return await _search_entities_like(db, Movie, norm, limit)


async def search_audio_work_fts(
    db: AsyncSession, query: str, limit: int = 30
) -> list[str]:
    """Search audio works by title. Returns a list of audio work entity IDs."""
    from app.models.audio_work import AudioWork

    norm = normalize_title(query)
    if not norm:
        return []
    return await _search_entities_like(db, AudioWork, norm, limit)
