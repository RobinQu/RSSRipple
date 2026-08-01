"""Tests for fts.py: FTS5 index upsert/search/delete/rebuild for
TVSeries, Movie, and AudioWork against real in-memory SQLite.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.audio_work import AudioWork
from app.services.fts import (
    delete_audio_work_fts,
    delete_movie_fts,
    delete_series_fts,
    rebuild_audio_work_fts,
    rebuild_movie_fts,
    rebuild_series_fts,
    search_audio_work_fts,
    search_movie_fts,
    search_series_fts,
    upsert_audio_work_fts,
    upsert_movie_fts,
    upsert_series_fts,
)

_FTS5 = os.environ.get("TEST_DB_BACKEND", "aiosqlite") != "aioturso"

# FTS5 index maintenance (upsert/delete/rebuild) is a no-op on backends
# without FTS5 — these tests assert FTS5-specific index behavior.
requires_fts5 = pytest.mark.skipif(
    not _FTS5, reason="FTS5 maintenance is a no-op without FTS5 (LIKE fallback)"
)
# LIKE-fallback tests only exercise the fallback on non-FTS5 backends.
requires_no_fts5 = pytest.mark.skipif(_FTS5, reason="LIKE fallback only without FTS5")


def _audio_work(**kw) -> AudioWork:
    defaults = dict(
        title_cn="深夜音声作品",
        title_en="Late Night Audio",
        original_title="Late Night Audio",
        aliases=["音声别名"],
        external_source="manual",
        content_type="asmr",
    )
    defaults.update(kw)
    return AudioWork(**defaults)


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------


@requires_fts5
async def test_series_fts_upsert_search_delete(db_session, sample_series):
    await upsert_series_fts(db_session, sample_series)

    # Trigram MATCH path (>= 3 chars after normalization)
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")
    # English title also indexed
    assert sample_series.id in await search_series_fts(db_session, "test series")
    # LIKE fallback for short queries (< 3 chars)
    assert sample_series.id in await search_series_fts(db_session, "测试")
    # Empty / whitespace query short-circuits
    assert await search_series_fts(db_session, "") == []
    assert await search_series_fts(db_session, "  ") == []

    await delete_series_fts(db_session, sample_series.id)
    assert sample_series.id not in await search_series_fts(db_session, "测试剧集")


async def test_series_fts_upsert_replaces_existing_row(db_session, sample_series):
    await upsert_series_fts(db_session, sample_series)
    sample_series.title_en = "Renamed Show"
    await upsert_series_fts(db_session, sample_series)

    assert sample_series.id in await search_series_fts(db_session, "renamed show")
    # Old row was deleted first — only one index entry for the entity
    hits = await search_series_fts(db_session, "test series")
    assert hits.count(sample_series.id) <= 1


async def test_search_fts_query_with_quotes_is_escaped(db_session, sample_series):
    await upsert_series_fts(db_session, sample_series)
    # Embedded double quotes must not break the FTS5 phrase query
    assert await search_series_fts(db_session, 'test "quoted" series') == []


@requires_fts5
async def test_rebuild_series_fts(db_session, sample_series):
    count = await rebuild_series_fts(db_session)
    assert count == 1
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")


# ---------------------------------------------------------------------------
# Movie
# ---------------------------------------------------------------------------


@requires_fts5
async def test_movie_fts_upsert_search_delete(db_session, sample_movie):
    await upsert_movie_fts(db_session, sample_movie)

    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")
    assert sample_movie.id in await search_movie_fts(db_session, "test movie")
    assert sample_movie.id in await search_movie_fts(db_session, "电影")  # LIKE path
    assert await search_movie_fts(db_session, "") == []

    await delete_movie_fts(db_session, sample_movie.id)
    assert sample_movie.id not in await search_movie_fts(db_session, "测试电影")


@requires_fts5
async def test_rebuild_movie_fts(db_session, sample_movie):
    count = await rebuild_movie_fts(db_session)
    assert count == 1
    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")


# ---------------------------------------------------------------------------
# AudioWork
# ---------------------------------------------------------------------------


@requires_fts5
async def test_audio_work_fts_upsert_search_delete(db_session):
    aw = _audio_work()
    db_session.add(aw)
    await db_session.flush()

    await upsert_audio_work_fts(db_session, aw)
    assert aw.id in await search_audio_work_fts(db_session, "深夜音声作品")
    assert aw.id in await search_audio_work_fts(db_session, "late night audio")
    # Alias text is indexed too (LIKE path for 2-char query)
    assert aw.id in await search_audio_work_fts(db_session, "别名")
    assert await search_audio_work_fts(db_session, "") == []

    await delete_audio_work_fts(db_session, aw.id)
    assert aw.id not in await search_audio_work_fts(db_session, "深夜音声作品")


@requires_fts5
async def test_rebuild_audio_work_fts(db_session):
    aw = _audio_work()
    db_session.add(aw)
    await db_session.flush()

    count = await rebuild_audio_work_fts(db_session)
    assert count == 1
    assert aw.id in await search_audio_work_fts(db_session, "深夜音声作品")


# ---------------------------------------------------------------------------
# Error swallowing: FTS helpers must log-and-continue on DB failures
# ---------------------------------------------------------------------------


async def test_upsert_swallows_db_errors():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("no such table")
    entity = SimpleNamespace(
        id="x", title_cn="t", title_en=None, original_title=None, aliases=None
    )
    await upsert_series_fts(db, entity)
    await upsert_movie_fts(db, entity)
    await upsert_audio_work_fts(db, entity)


async def test_delete_swallows_db_errors():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("no such table")
    await delete_series_fts(db, "x")
    await delete_movie_fts(db, "x")
    await delete_audio_work_fts(db, "x")


@requires_fts5
async def test_search_swallows_db_errors():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("no such table")
    for search in (search_series_fts, search_movie_fts, search_audio_work_fts):
        assert await search(db, "long enough query") == []  # MATCH path
        assert await search(db, "ab") == []  # LIKE path


# ---------------------------------------------------------------------------
# LIKE fallback (backends without FTS5, e.g. Turso)
# ---------------------------------------------------------------------------


@requires_no_fts5
async def test_fallback_search_reads_base_table(db_session, sample_series, sample_movie):
    # No FTS index maintenance happens — search reads the base tables directly.
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")
    assert sample_series.id in await search_series_fts(db_session, "test series")
    assert sample_series.id in await search_series_fts(db_session, "测试")
    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")
    assert sample_movie.id in await search_movie_fts(db_session, "test movie")
    assert await search_series_fts(db_session, "") == []


@requires_no_fts5
async def test_fallback_rebuild_is_noop(db_session, sample_series):
    assert await rebuild_series_fts(db_session) == 0
    assert await rebuild_movie_fts(db_session) == 0
    assert await rebuild_audio_work_fts(db_session) == 0
    # ...but search still works off the base table.
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")
