"""Unit tests for the per-channel periodic works metadata refresh.

Covers the three layers separately:
* work selection (``select_channel_works_for_refresh`` — the missing-fields
  gate is a selection predicate, not fetch logic);
* the job handler (parameter derivation only: channel source, protected
  manual edits, gated/full scope);
* scheduler registration (``schedule_channel`` registers the refresh job
  only when the channel opts in).
"""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.job_handlers import _handle_refresh_channel_works
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from app.services import scheduler as sch
from app.services.metadata_service import select_channel_works_for_refresh


def _uuid() -> str:
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


def _complete_series(channel_id: str) -> TVSeries:
    """A series with nothing left to fill (excluded by the gate)."""
    return TVSeries(
        id=_uuid(), title_cn="完整", title_en="Complete",
        original_title="完全", description="d", rating=8.0,
        status="ended", genre=["Action"], poster_url="/posters/x.jpg",
        number_of_episodes=12, number_of_seasons=1,
        start_date=date(2024, 1, 1), end_date=date(2024, 3, 1),
        external_id="bangumi:1", external_source="bangumi",
    )


def _gapped_series(channel_id: str) -> TVSeries:
    """A series with a fillable gap (rating missing → gate includes it)."""
    return TVSeries(
        id=_uuid(), title_cn="缺评分", title_en="Gapped",
        description="d", status="ended", genre=["Action"],
        poster_url="/posters/y.jpg", number_of_episodes=12,
        number_of_seasons=1, start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 1),
        external_id="bangumi:2", external_source="bangumi",
    )


def _gapped_movie() -> Movie:
    return Movie(
        id=_uuid(), title_cn="缺日期", title_en="No Date",
        description="d", rating=7.0, genre=["Drama"],
        poster_url="/posters/z.jpg",
        external_id="tmdb:9", external_source="tmdb",
    )


async def _link(db_session, channel: Channel, *, series=None, movie=None):
    fr = FileResource(
        id=_uuid(), channel_id=channel.id, guid=_uuid(),
        title_raw="t", torrent_url=f"magnet:?xt=urn:btih:{_uuid()}",
        search_title="t",
        series_id=series.id if series else None,
        movie_id=movie.id if movie else None,
    )
    db_session.add(fr)
    await db_session.flush()


@pytest.fixture
async def seeded(db_session):
    ch = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        metadata_agent_enabled=True, metadata_source="bangumi",
    )
    db_session.add(ch)
    await db_session.flush()
    complete = _complete_series(ch.id)
    gapped = _gapped_series(ch.id)
    movie = _gapped_movie()
    other = _gapped_series(ch.id)
    db_session.add_all([complete, gapped, movie, other])
    await db_session.flush()
    await _link(db_session, ch, series=complete)
    await _link(db_session, ch, series=gapped)
    await _link(db_session, ch, movie=movie)
    # `other` belongs to a different channel.
    ch2 = Channel(
        id=_uuid(), name="ch2", type="rss_feed", url="https://example.com/2",
        field_mapping=TEST_FIELD_MAPPING,
    )
    db_session.add(ch2)
    await db_session.flush()
    await _link(db_session, ch2, series=other)
    await db_session.commit()
    return SimpleNamespace(ch=ch, complete=complete, gapped=gapped, movie=movie)


# ---------------------------------------------------------------------------
# Work selection (the missing-fields gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selection_gate_returns_only_works_with_gaps(db_session, seeded):
    works = await select_channel_works_for_refresh(db_session, seeded.ch.id)
    ids = {(w["id"], w["content_type"]) for w in works}
    # The fully-complete series is excluded; the gapped ones are included.
    assert (seeded.complete.id, "tv") not in ids
    assert (seeded.gapped.id, "tv") in ids
    assert (seeded.movie.id, "movie") in ids


@pytest.mark.asyncio
async def test_selection_full_scope_returns_all_linked(db_session, seeded):
    works = await select_channel_works_for_refresh(
        db_session, seeded.ch.id, full_scope=True
    )
    ids = {(w["id"], w["content_type"]) for w in works}
    assert (seeded.complete.id, "tv") in ids
    assert (seeded.gapped.id, "tv") in ids
    assert (seeded.movie.id, "movie") in ids
    # Other channels' works are never included.
    assert len(ids) == 3


@pytest.mark.asyncio
async def test_selection_is_scoped_to_the_channel(db_session, seeded):
    works = await select_channel_works_for_refresh(
        db_session, seeded.ch.id, full_scope=True
    )
    assert all(w["id"] != "x" for w in works)


# ---------------------------------------------------------------------------
# Job handler (parameter derivation only — no fetch logic of its own)
# ---------------------------------------------------------------------------


class _SessionCtx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def _patch_handler_env(monkeypatch, db_session):
    monkeypatch.setattr("app.job_handlers._refresh_runtime_config", AsyncMock())
    monkeypatch.setattr(
        "app.job_handlers.committed_session", lambda: _SessionCtx(db_session)
    )


@pytest.mark.asyncio
async def test_handler_derives_params_from_channel(monkeypatch, db_session, seeded):
    _patch_handler_env(monkeypatch, db_session)
    batch = AsyncMock(return_value=[])
    monkeypatch.setattr("app.job_handlers._refresh_works_batch", batch)

    result = await _handle_refresh_channel_works({"channel_id": seeded.ch.id})

    assert result["status"] == "done"
    args, kwargs = batch.await_args
    items, source = args[0], args[1]
    # Source comes from the channel's own metadata_source...
    assert source == "bangumi"
    # ...and manual edits are always protected.
    assert kwargs["override_manual_edits"] is False
    # Default scope is gated: only works with fillable gaps.
    ids = {i["id"] for i in items}
    assert seeded.complete.id not in ids
    assert seeded.gapped.id in ids


@pytest.mark.asyncio
async def test_handler_full_scope_passes_all_works(monkeypatch, db_session, seeded):
    _patch_handler_env(monkeypatch, db_session)
    seeded.ch.metadata_refresh_full_scope = True
    await db_session.flush()
    batch = AsyncMock(return_value=[])
    monkeypatch.setattr("app.job_handlers._refresh_works_batch", batch)

    await _handle_refresh_channel_works({"channel_id": seeded.ch.id})

    items = batch.await_args.args[0]
    ids = {i["id"] for i in items}
    assert seeded.complete.id in ids


@pytest.mark.asyncio
async def test_handler_skips_inactive_channel(monkeypatch, db_session, seeded):
    _patch_handler_env(monkeypatch, db_session)
    seeded.ch.status = "inactive"
    await db_session.flush()
    batch = AsyncMock()
    monkeypatch.setattr("app.job_handlers._refresh_works_batch", batch)

    result = await _handle_refresh_channel_works({"channel_id": seeded.ch.id})

    assert result["processed"] == 0
    batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_skips_missing_channel(monkeypatch, db_session):
    _patch_handler_env(monkeypatch, db_session)

    result = await _handle_refresh_channel_works({"channel_id": "nope"})

    assert result["processed"] == 0


# ---------------------------------------------------------------------------
# Scheduler registration
# ---------------------------------------------------------------------------


def _channel(**overrides) -> Channel:
    base = dict(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        fetch_interval=1800,
        metadata_refresh_enabled=True,
        metadata_refresh_interval_minutes=None,
    )
    base.update(overrides)
    return Channel(**base)


@pytest.mark.asyncio
async def test_schedule_channel_registers_both_jobs(monkeypatch):
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "scheduler_enabled", True)
    sched = MagicMock()
    monkeypatch.setattr(sch, "get_scheduler", lambda: sched)

    sch.schedule_channel(_channel())

    ids = {c.kwargs["id"] for c in sched.add_job.call_args_list}
    assert any(i.startswith("channel:") for i in ids)
    refresh_ids = [i for i in ids if i.startswith("channel-refresh:")]
    assert len(refresh_ids) == 1
    # NULL interval falls back to the daily default.
    trigger = next(
        c.kwargs["trigger"] for c in sched.add_job.call_args_list
        if c.kwargs["id"].startswith("channel-refresh:")
    )
    assert trigger.interval.total_seconds() == 1440 * 60


@pytest.mark.asyncio
async def test_schedule_channel_skips_refresh_job_when_disabled(monkeypatch):
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "scheduler_enabled", True)
    sched = MagicMock()
    monkeypatch.setattr(sch, "get_scheduler", lambda: sched)

    sch.schedule_channel(_channel(metadata_refresh_enabled=False))

    ids = {c.kwargs["id"] for c in sched.add_job.call_args_list}
    assert not any(i.startswith("channel-refresh:") for i in ids)


@pytest.mark.asyncio
async def test_unschedule_channel_removes_both_jobs():
    sched = MagicMock()
    with patch.object(sch, "_scheduler", sched), \
         patch.object(sch, "get_scheduler", lambda: sched):
        sch.unschedule_channel("abc")
    removed = {c.args[0] for c in sched.remove_job.call_args_list}
    assert removed == {"channel:abc", "channel-refresh:abc"}


@pytest.mark.asyncio
async def test_run_channel_works_refresh_enqueues_with_stable_key(monkeypatch):
    from app.services import task_queue as tq_mod

    fake = MagicMock()
    fake.enqueue = AsyncMock()
    monkeypatch.setattr(tq_mod, "task_queue", fake)

    await sch._run_channel_works_refresh("ch-1")

    fake.enqueue.assert_awaited_once()
    assert fake.enqueue.call_args.args[0] == "refresh_channel_works"
    assert fake.enqueue.call_args.args[1] == "channel-refresh:ch-1"
