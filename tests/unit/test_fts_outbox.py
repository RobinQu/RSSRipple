"""Tests for outbox-driven FTS sync (Turso) and search_text (PostgreSQL).

Covers the ORM before_flush/after_flush hooks that enqueue ``fts_outbox``
rows atomically with work-row changes, the drain replay onto the sidecar
shadow tables, the single-char ngram fallback, and the ``search_text`` column
maintenance/backfill shared by both backends.
"""

import uuid

from sqlalchemy import select

from app.models.audio_work import AudioWork
from app.models.fts_outbox import FtsOutbox
from app.models.series import TVSeries
from app.services.fts import (
    backfill_search_text,
    drain_fts_outbox,
    search_audio_work_fts,
    search_movie_fts,
    search_series_fts,
)


async def _outbox_for(db_session, entity_id: str) -> list[str]:
    result = await db_session.execute(
        select(FtsOutbox.op).where(FtsOutbox.entity_id == entity_id)
    )
    return [r[0] for r in result.all()]


async def _outbox_rows(db_session):
    result = await db_session.execute(select(FtsOutbox))
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Enqueueing (Turso)
# ---------------------------------------------------------------------------


async def test_create_series_enqueues_upsert(db_session, sample_series):
    await db_session.commit()
    assert await _outbox_for(db_session, sample_series.id) == ["upsert"]


async def test_update_enqueues_upsert(db_session, sample_series):
    sample_series.title_en = "Renamed"
    await db_session.flush()
    await db_session.commit()
    ops = await _outbox_for(db_session, sample_series.id)
    assert "upsert" in ops
    assert "delete" not in ops


async def test_delete_enqueues_delete(db_session, sample_series):
    await db_session.commit()
    await db_session.delete(sample_series)
    await db_session.commit()
    assert "delete" in await _outbox_for(db_session, sample_series.id)


async def test_rollback_discards_outbox(db_session):
    s = TVSeries(
        id=str(uuid.uuid4()),
        title_cn="回滚剧集",
        title_en="Rollback Series",
        content_type="tv",
    )
    db_session.add(s)
    await db_session.flush()
    await db_session.rollback()
    assert await _outbox_for(db_session, s.id) == []


async def test_repeated_autoflush_enqueues_once(db_session, sample_series):
    sample_series.rating = 8.0
    await db_session.flush()
    sample_series.rating = 9.0
    await db_session.flush()
    await db_session.commit()
    assert len(await _outbox_for(db_session, sample_series.id)) == 1


async def test_search_text_maintained_on_turso(db_session, sample_series):
    await db_session.commit()
    assert sample_series.search_text == "测试剧集 test series test series 别名"


async def test_collection_search_text_maintained_without_outbox(db_session):
    """WorkCollection rows get search_text from the same before_flush hook
    (title_cn/title_en/aliases through normalize_title) but are NEVER
    enqueued into fts_outbox — the FTS sidecar covers work tables only."""
    from app.models.work_collection import WorkCollection

    c = WorkCollection(
        id=str(uuid.uuid4()),
        title_cn="無職転生",
        title_en="Mushoku Tensei",
        aliases=["无职转生"],
    )
    db_session.add(c)
    await db_session.commit()
    assert c.search_text is not None
    assert "mushoku tensei" in c.search_text
    assert "无职转生" in c.search_text  # OpenCC t2s folds 無職転生
    assert await _outbox_for(db_session, c.id) == []

    c.title_en = "Mushoku Tensei: Jobless Reincarnation"
    await db_session.commit()
    assert "jobless reincarnation" in c.search_text
    assert await _outbox_for(db_session, c.id) == []


# ---------------------------------------------------------------------------
# Drain (Turso sidecar replay)
# ---------------------------------------------------------------------------


async def test_drain_replays_shadow_and_clears_outbox(db_session, sample_series):
    await db_session.commit()
    n = await drain_fts_outbox(db_session)
    await db_session.commit()
    assert n == 1
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")
    assert await _outbox_rows(db_session) == []


async def test_drain_tracks_updates(db_session, sample_series):
    await db_session.commit()
    await drain_fts_outbox(db_session)
    await db_session.commit()
    sample_series.title_en = "Renamed Show"
    sample_series.original_title = "Renamed Original"
    await db_session.flush()
    await db_session.commit()
    await drain_fts_outbox(db_session)
    await db_session.commit()
    assert sample_series.id in await search_series_fts(db_session, "renamed")
    assert sample_series.id not in await search_series_fts(db_session, "test series")


async def test_drain_deletes_shadow_row(db_session, sample_series):
    await db_session.commit()
    await drain_fts_outbox(db_session)
    await db_session.commit()
    await db_session.delete(sample_series)
    await db_session.commit()
    await drain_fts_outbox(db_session)
    await db_session.commit()
    assert sample_series.id not in await search_series_fts(db_session, "测试剧集")


async def test_drain_upsert_for_missing_entity_becomes_delete(db_session, sample_series):
    """An upsert row whose entity vanished before the drain becomes a delete."""
    await db_session.commit()
    await drain_fts_outbox(db_session)
    await db_session.commit()
    # Remove the base row behind the ORM's back (script-like path)...
    await db_session.execute(
        TVSeries.__table__.delete().where(TVSeries.id == sample_series.id)
    )
    await db_session.commit()
    # ...so no delete was ever enqueued; plant a stale upsert row instead.
    db_session.add(FtsOutbox(
        entity_type="series", entity_id=sample_series.id, op="upsert"
    ))
    await db_session.commit()
    await drain_fts_outbox(db_session)
    await db_session.commit()
    assert sample_series.id not in await search_series_fts(db_session, "测试剧集")


async def test_drain_all_three_work_types(db_session, sample_series, sample_movie):
    aw = AudioWork(
        id=str(uuid.uuid4()),
        title_cn="深夜音声作品",
        title_en="Late Night Audio",
        original_title="Late Night Audio",
        aliases=["音声别名"],
        external_source="manual",
        content_type="asmr",
    )
    db_session.add(aw)
    await db_session.commit()

    n = await drain_fts_outbox(db_session)
    await db_session.commit()
    assert n == 3
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")
    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")
    assert aw.id in await search_audio_work_fts(db_session, "深夜音声作品")
    assert aw.id in await search_audio_work_fts(db_session, "别名")
    assert await _outbox_rows(db_session) == []


# ---------------------------------------------------------------------------
# Dedup merge (via ORM deletes/updates → outbox)
# ---------------------------------------------------------------------------


async def test_dedup_merge_enqueues_survivor_upsert_and_dup_delete(db_session):
    from app.services.metadata_dedup import merge_duplicate_metadata

    s1 = TVSeries(
        id=str(uuid.uuid4()), title_cn="消歧剧集", title_en="Dedup Series",
        external_source="manual", content_type="tv",
    )
    s2 = TVSeries(
        id=str(uuid.uuid4()), title_cn="消歧剧集", title_en="Dedup Series",
        external_source="manual", content_type="tv",
    )
    db_session.add_all([s1, s2])
    await db_session.commit()

    await merge_duplicate_metadata(db_session)
    await db_session.commit()

    survivor = await db_session.get(TVSeries, s1.id)
    dup = s2.id if survivor is not None else s1.id
    survivor_id = s1.id if survivor is not None else s2.id
    assert "upsert" in await _outbox_for(db_session, survivor_id)
    assert "delete" in await _outbox_for(db_session, dup)


# ---------------------------------------------------------------------------
# Single-char fallback (Turso ngram emits no tokens for len < 2)
# ---------------------------------------------------------------------------


async def test_single_char_search_falls_back_to_python_scan(db_session, sample_series):
    await db_session.commit()
    assert sample_series.id in await search_series_fts(db_session, "试")


# ---------------------------------------------------------------------------
# search_text backfill
# ---------------------------------------------------------------------------


async def test_backfill_search_text_fills_null_rows(db_session, sample_series):
    await db_session.commit()
    await db_session.execute(
        TVSeries.__table__.update()
        .where(TVSeries.id == sample_series.id)
        .values(search_text=None)
    )
    await db_session.commit()
    row = (await db_session.execute(
        select(TVSeries)
        .where(TVSeries.id == sample_series.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert row.search_text is None

    n = await backfill_search_text(db_session)
    await db_session.commit()
    assert n >= 1
    row = (await db_session.execute(
        select(TVSeries)
        .where(TVSeries.id == sample_series.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert row.search_text == "测试剧集 test series test series 别名"
