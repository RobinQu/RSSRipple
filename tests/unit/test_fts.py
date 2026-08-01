"""Tests for fts.py: Turso native FTS (ngram) upsert/search/delete/rebuild
for TVSeries, Movie, and AudioWork against a real Turso database.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.audio_work import AudioWork
from app.services.fts import (
    backfill_fts_if_empty,
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


async def test_series_fts_upsert_search_delete(db_session, sample_series):
    await upsert_series_fts(db_session, sample_series)

    # ngram substring path (>= 2 chars after normalization)
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")
    # substring of the title also matches
    assert sample_series.id in await search_series_fts(db_session, "试剧")
    # English title also indexed (case-insensitive via normalization)
    assert sample_series.id in await search_series_fts(db_session, "test series")
    assert sample_series.id in await search_series_fts(db_session, "TEST SERIES")
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


async def test_rebuild_series_fts(db_session, sample_series):
    count = await rebuild_series_fts(db_session)
    assert count == 1
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")


# ---------------------------------------------------------------------------
# Movie
# ---------------------------------------------------------------------------


async def test_movie_fts_upsert_search_delete(db_session, sample_movie):
    await upsert_movie_fts(db_session, sample_movie)

    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")
    assert sample_movie.id in await search_movie_fts(db_session, "test movie")
    assert sample_movie.id in await search_movie_fts(db_session, "电影")
    assert await search_movie_fts(db_session, "") == []

    await delete_movie_fts(db_session, sample_movie.id)
    assert sample_movie.id not in await search_movie_fts(db_session, "测试电影")


async def test_rebuild_movie_fts(db_session, sample_movie):
    count = await rebuild_movie_fts(db_session)
    assert count == 1
    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")


# ---------------------------------------------------------------------------
# AudioWork
# ---------------------------------------------------------------------------


async def test_audio_work_fts_upsert_search_delete(db_session):
    aw = _audio_work()
    db_session.add(aw)
    await db_session.flush()

    await upsert_audio_work_fts(db_session, aw)
    assert aw.id in await search_audio_work_fts(db_session, "深夜音声作品")
    assert aw.id in await search_audio_work_fts(db_session, "late night audio")
    # Alias text is indexed too
    assert aw.id in await search_audio_work_fts(db_session, "别名")
    assert await search_audio_work_fts(db_session, "") == []

    await delete_audio_work_fts(db_session, aw.id)
    assert aw.id not in await search_audio_work_fts(db_session, "深夜音声作品")


async def test_rebuild_audio_work_fts(db_session):
    aw = _audio_work()
    db_session.add(aw)
    await db_session.flush()

    count = await rebuild_audio_work_fts(db_session)
    assert count == 1
    assert aw.id in await search_audio_work_fts(db_session, "深夜音声作品")


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


async def test_backfill_fts_if_empty(db_session, sample_series, sample_movie):
    await backfill_fts_if_empty(db_session)
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")
    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")


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


async def test_search_swallows_db_errors():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("no such table")
    for search in (search_series_fts, search_movie_fts, search_audio_work_fts):
        assert await search(db, "long enough query") == []
        assert await search(db, "ab") == []


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


async def test_reconcile_fts_heals_divergence(db_session, sample_series, sample_movie):
    from sqlalchemy import text

    from app.services.fts import _get_fts_engine, reconcile_fts

    await backfill_fts_if_empty(db_session)

    engine = _get_fts_engine()
    async with engine.begin() as conn:
        # 1. stale content on an existing shadow row
        await conn.execute(text(
            "UPDATE tv_series_fts SET title_en = 'stale title' WHERE entity_id = :id"
        ), {"id": sample_series.id})
        # 2. orphan shadow row (base row does not exist)
        await conn.execute(text(
            "INSERT INTO tv_series_fts (entity_id, title_cn) VALUES ('orphan-id', '幽灵')"
        ))
        # 3. missing shadow row (movie deleted from shadow)
        await conn.execute(text(
            "DELETE FROM movie_fts WHERE entity_id = :id"
        ), {"id": sample_movie.id})

    report = await reconcile_fts(db_session)
    assert report["updated"] == 2  # stale series row + missing movie row
    assert report["deleted"] == 1  # orphan

    assert sample_series.id in await search_series_fts(db_session, "test series")
    assert sample_series.id not in await search_series_fts(db_session, "stale")
    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")
    assert "orphan-id" not in await search_series_fts(db_session, "幽灵")


async def test_reconcile_fts_noop_when_in_sync(db_session, sample_series):
    from app.services.fts import reconcile_fts

    await backfill_fts_if_empty(db_session)
    report = await reconcile_fts(db_session)
    assert report == {"updated": 0, "deleted": 0}
