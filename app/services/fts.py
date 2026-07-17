"""SQLite FTS5 full-text search index management for TVSeries and Movie.

Uses the ``trigram`` tokenizer (built into SQLite ≥ 3.34) for CJK-friendly
substring matching without requiring a word segmenter.

Design:
- **Standalone FTS5 tables** with an ``entity_id`` UNINDEXED column for the
  string UUID primary key. FTS5 auto-generates integer rowids.
- **Normalized content** — all indexed text is passed through
  ``text_normalizer.normalize_title`` (NFKC + OpenCC t2s + lowercase) so that
  Traditional/Simplified, half/full-width, and case variants all match.
- **Candidate retrieval + scoring** — FTS5 MATCH retrieves candidates; callers
  compute ``similarity_score`` for precise ranking.

For queries shorter than 3 characters (no trigrams), falls back to ``LIKE`` on
the FTS table columns.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.services.text_normalizer import normalize_title

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_SERIES_FTS = """
    CREATE VIRTUAL TABLE IF NOT EXISTS tv_series_fts USING fts5(
        entity_id UNINDEXED,
        title_cn, title_en, original_title, aliases,
        tokenize='trigram'
    )
"""

_CREATE_MOVIE_FTS = """
    CREATE VIRTUAL TABLE IF NOT EXISTS movie_fts USING fts5(
        entity_id UNINDEXED,
        title_cn, title_en, original_title, aliases,
        tokenize='trigram'
    )
"""

_CREATE_AUDIO_WORK_FTS = """
    CREATE VIRTUAL TABLE IF NOT EXISTS audio_work_fts USING fts5(
        entity_id UNINDEXED,
        title_cn, title_en, original_title, aliases,
        tokenize='trigram'
    )
"""


async def ensure_fts_tables(conn: AsyncConnection) -> None:
    """Create FTS5 virtual tables if they don't exist (SQLite only)."""
    try:
        await conn.execute(text(_CREATE_SERIES_FTS))
        await conn.execute(text(_CREATE_MOVIE_FTS))
        await conn.execute(text(_CREATE_AUDIO_WORK_FTS))
    except Exception as e:
        # FTS5 or trigram tokenizer might not be available in all SQLite builds
        logger.warning("[fts] Could not create FTS5 tables: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fts_values(entity: Any) -> dict[str, str]:
    """Extract normalized title fields from a TVSeries or Movie for FTS indexing."""
    return {
        "title_cn": normalize_title(entity.title_cn) or "",
        "title_en": normalize_title(entity.title_en) or "",
        "original_title": normalize_title(entity.original_title) or "",
        "aliases": " ".join(
            normalize_title(a) for a in (entity.aliases or []) if a
        ),
    }


def _escape_fts_query(s: str) -> str:
    """Escape a string for use as an FTS5 phrase query.

    Wraps in double quotes and doubles any internal double quotes.
    """
    return '"' + s.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Series FTS
# ---------------------------------------------------------------------------


async def upsert_series_fts(db: AsyncSession, series: Any) -> None:
    """Insert or update a series in the FTS index."""
    try:
        # Delete existing entry for this entity
        await db.execute(
            text("DELETE FROM tv_series_fts WHERE entity_id = :id"),
            {"id": series.id},
        )
        vals = _fts_values(series)
        vals["id"] = series.id
        await db.execute(
            text(
                "INSERT INTO tv_series_fts (entity_id, title_cn, title_en, original_title, aliases) "
                "VALUES (:id, :title_cn, :title_en, :original_title, :aliases)"
            ),
            vals,
        )
    except Exception as e:
        logger.warning("[fts] upsert_series_fts failed for %s: %s", series.id, e)


async def delete_series_fts(db: AsyncSession, series_id: str) -> None:
    """Remove a series from the FTS index."""
    try:
        await db.execute(
            text("DELETE FROM tv_series_fts WHERE entity_id = :id"),
            {"id": series_id},
        )
    except Exception as e:
        logger.warning("[fts] delete_series_fts failed for %s: %s", series_id, e)


async def search_series_fts(
    db: AsyncSession, query: str, limit: int = 30
) -> list[str]:
    """Search series via FTS5. Returns a list of series entity IDs."""
    norm = normalize_title(query)
    if not norm:
        return []

    # For queries < 3 chars, trigram tokenizer produces no tokens — use LIKE
    if len(norm) < 3:
        try:
            pattern = f"%{norm}%"
            result = await db.execute(
                text(
                    "SELECT entity_id FROM tv_series_fts "
                    "WHERE title_cn LIKE :p OR title_en LIKE :p "
                    "OR original_title LIKE :p OR aliases LIKE :p "
                    "LIMIT :limit"
                ),
                {"p": pattern, "limit": limit},
            )
            return [row[0] for row in result.fetchall()]
        except Exception as e:
            logger.warning("[fts] search_series_fts LIKE fallback failed: %s", e)
            return []

    fts_query = _escape_fts_query(norm)
    try:
        result = await db.execute(
            text(
                "SELECT entity_id FROM tv_series_fts "
                "WHERE tv_series_fts MATCH :query "
                "ORDER BY bm25(tv_series_fts) "
                "LIMIT :limit"
            ),
            {"query": fts_query, "limit": limit},
        )
        return [row[0] for row in result.fetchall()]
    except Exception as e:
        logger.warning("[fts] search_series_fts MATCH failed for %r: %s", norm[:60], e)
        return []


async def rebuild_series_fts(db: AsyncSession) -> int:
    """Rebuild the entire series FTS index from the tv_series table."""
    from app.models.series import TVSeries

    try:
        await db.execute(text("DELETE FROM tv_series_fts"))
    except Exception:
        pass
    result = await db.execute(select(TVSeries))
    count = 0
    for series in result.scalars().all():
        await upsert_series_fts(db, series)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Movie FTS
# ---------------------------------------------------------------------------


async def upsert_movie_fts(db: AsyncSession, movie: Any) -> None:
    """Insert or update a movie in the FTS index."""
    try:
        await db.execute(
            text("DELETE FROM movie_fts WHERE entity_id = :id"),
            {"id": movie.id},
        )
        vals = _fts_values(movie)
        vals["id"] = movie.id
        await db.execute(
            text(
                "INSERT INTO movie_fts (entity_id, title_cn, title_en, original_title, aliases) "
                "VALUES (:id, :title_cn, :title_en, :original_title, :aliases)"
            ),
            vals,
        )
    except Exception as e:
        logger.warning("[fts] upsert_movie_fts failed for %s: %s", movie.id, e)


async def delete_movie_fts(db: AsyncSession, movie_id: str) -> None:
    """Remove a movie from the FTS index."""
    try:
        await db.execute(
            text("DELETE FROM movie_fts WHERE entity_id = :id"),
            {"id": movie_id},
        )
    except Exception as e:
        logger.warning("[fts] delete_movie_fts failed for %s: %s", movie_id, e)


async def search_movie_fts(
    db: AsyncSession, query: str, limit: int = 30
) -> list[str]:
    """Search movies via FTS5. Returns a list of movie entity IDs."""
    norm = normalize_title(query)
    if not norm:
        return []

    if len(norm) < 3:
        try:
            pattern = f"%{norm}%"
            result = await db.execute(
                text(
                    "SELECT entity_id FROM movie_fts "
                    "WHERE title_cn LIKE :p OR title_en LIKE :p "
                    "OR original_title LIKE :p OR aliases LIKE :p "
                    "LIMIT :limit"
                ),
                {"p": pattern, "limit": limit},
            )
            return [row[0] for row in result.fetchall()]
        except Exception as e:
            logger.warning("[fts] search_movie_fts LIKE fallback failed: %s", e)
            return []

    fts_query = _escape_fts_query(norm)
    try:
        result = await db.execute(
            text(
                "SELECT entity_id FROM movie_fts "
                "WHERE movie_fts MATCH :query "
                "ORDER BY bm25(movie_fts) "
                "LIMIT :limit"
            ),
            {"query": fts_query, "limit": limit},
        )
        return [row[0] for row in result.fetchall()]
    except Exception as e:
        logger.warning("[fts] search_movie_fts MATCH failed for %r: %s", norm[:60], e)
        return []


async def rebuild_movie_fts(db: AsyncSession) -> int:
    """Rebuild the entire movie FTS index from the movies table."""
    from app.models.movie import Movie

    try:
        await db.execute(text("DELETE FROM movie_fts"))
    except Exception:
        pass
    result = await db.execute(select(Movie))
    count = 0
    for movie in result.scalars().all():
        await upsert_movie_fts(db, movie)
        count += 1
    return count


# ---------------------------------------------------------------------------
# AudioWork FTS
# ---------------------------------------------------------------------------


async def upsert_audio_work_fts(db: AsyncSession, audio_work: Any) -> None:
    """Insert or update an audio work in the FTS index."""
    try:
        await db.execute(
            text("DELETE FROM audio_work_fts WHERE entity_id = :id"),
            {"id": audio_work.id},
        )
        vals = _fts_values(audio_work)
        vals["id"] = audio_work.id
        await db.execute(
            text(
                "INSERT INTO audio_work_fts (entity_id, title_cn, title_en, original_title, aliases) "
                "VALUES (:id, :title_cn, :title_en, :original_title, :aliases)"
            ),
            vals,
        )
    except Exception as e:
        logger.warning("[fts] upsert_audio_work_fts failed for %s: %s", audio_work.id, e)


async def delete_audio_work_fts(db: AsyncSession, audio_work_id: str) -> None:
    """Remove an audio work from the FTS index."""
    try:
        await db.execute(
            text("DELETE FROM audio_work_fts WHERE entity_id = :id"),
            {"id": audio_work_id},
        )
    except Exception as e:
        logger.warning("[fts] delete_audio_work_fts failed for %s: %s", audio_work_id, e)


async def search_audio_work_fts(
    db: AsyncSession, query: str, limit: int = 30
) -> list[str]:
    """Search audio works via FTS5. Returns a list of audio work entity IDs."""
    norm = normalize_title(query)
    if not norm:
        return []

    if len(norm) < 3:
        try:
            pattern = f"%{norm}%"
            result = await db.execute(
                text(
                    "SELECT entity_id FROM audio_work_fts "
                    "WHERE title_cn LIKE :p OR title_en LIKE :p "
                    "OR original_title LIKE :p OR aliases LIKE :p "
                    "LIMIT :limit"
                ),
                {"p": pattern, "limit": limit},
            )
            return [row[0] for row in result.fetchall()]
        except Exception as e:
            logger.warning("[fts] search_audio_work_fts LIKE fallback failed: %s", e)
            return []

    fts_query = _escape_fts_query(norm)
    try:
        result = await db.execute(
            text(
                "SELECT entity_id FROM audio_work_fts "
                "WHERE audio_work_fts MATCH :query "
                "ORDER BY bm25(audio_work_fts) "
                "LIMIT :limit"
            ),
            {"query": fts_query, "limit": limit},
        )
        return [row[0] for row in result.fetchall()]
    except Exception as e:
        logger.warning("[fts] search_audio_work_fts MATCH failed for %r: %s", norm[:60], e)
        return []


async def rebuild_audio_work_fts(db: AsyncSession) -> int:
    """Rebuild the entire audio work FTS index from the audio_works table."""
    from app.models.audio_work import AudioWork

    try:
        await db.execute(text("DELETE FROM audio_work_fts"))
    except Exception:
        pass
    result = await db.execute(select(AudioWork))
    count = 0
    for aw in result.scalars().all():
        await upsert_audio_work_fts(db, aw)
        count += 1
    return count


# Need select import for rebuild functions
from sqlalchemy import select  # noqa: E402
