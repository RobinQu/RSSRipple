"""Tests for fts.py: normalized substring search for TVSeries, Movie, and
AudioWork against a real (Turso) database.
"""

from unittest.mock import AsyncMock

from app.models.audio_work import AudioWork
from app.services.fts import (
    search_audio_work_fts,
    search_movie_fts,
    search_series_fts,
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


async def test_search_series(db_session, sample_series):
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")
    # English title also matches
    assert sample_series.id in await search_series_fts(db_session, "test series")
    # Short queries work too (no tokenizer minimum)
    assert sample_series.id in await search_series_fts(db_session, "测试")
    # Empty / whitespace query short-circuits
    assert await search_series_fts(db_session, "") == []
    assert await search_series_fts(db_session, "  ") == []
    # Non-matching query
    assert sample_series.id not in await search_series_fts(db_session, "不存在的标题")


async def test_search_movie(db_session, sample_movie):
    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")
    assert sample_movie.id in await search_movie_fts(db_session, "test movie")
    assert sample_movie.id in await search_movie_fts(db_session, "电影")
    assert await search_movie_fts(db_session, "") == []


async def test_search_audio_work(db_session):
    aw = _audio_work()
    db_session.add(aw)
    await db_session.flush()

    assert aw.id in await search_audio_work_fts(db_session, "深夜音声作品")
    assert aw.id in await search_audio_work_fts(db_session, "late night audio")
    # Alias text is searchable too
    assert aw.id in await search_audio_work_fts(db_session, "别名")
    assert await search_audio_work_fts(db_session, "") == []


async def test_search_normalization(db_session, sample_series):
    # Case and width variants normalize to the same form.
    assert sample_series.id in await search_series_fts(db_session, "TEST SERIES")


async def test_search_swallows_db_errors():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("boom")
    for search in (search_series_fts, search_movie_fts, search_audio_work_fts):
        assert await search(db, "long enough query") == []
        assert await search(db, "ab") == []
