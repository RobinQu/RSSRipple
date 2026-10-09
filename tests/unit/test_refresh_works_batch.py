"""Unit tests for the background metadata-refresh jobs.

``_handle_refresh_works_metadata`` (manual batch) derives parameters and
funnels into ``_refresh_works_batch``; the batch loop wraps
``refresh_work_by_source`` per work with a timeout and per-work error
isolation. The channel-scoped periodic handler lives in
test_channel_works_refresh.py.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from app.job_handlers import _handle_refresh_works_metadata, _refresh_works_batch
from app.models.series import TVSeries


def _uuid() -> str:
    return str(uuid.uuid4())


class _SessionCtx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def _patch_env(monkeypatch, db_session):
    monkeypatch.setattr("app.job_handlers._refresh_runtime_config", AsyncMock())
    monkeypatch.setattr(
        "app.job_handlers.committed_session", lambda: _SessionCtx(db_session)
    )


# ---------------------------------------------------------------------------
# _handle_refresh_works_metadata (manual batch refresh job)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_refresh_works_requires_source(monkeypatch, db_session):
    _patch_env(monkeypatch, db_session)
    result = await _handle_refresh_works_metadata({"items": [{"id": "x"}]})
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_handle_refresh_works_derives_params(monkeypatch, db_session):
    _patch_env(monkeypatch, db_session)
    batch = AsyncMock(return_value=[{"id": "w1", "found": True}])
    monkeypatch.setattr("app.job_handlers._refresh_works_batch", batch)

    result = await _handle_refresh_works_metadata({
        "items": [{"id": "w1", "content_type": "tv"}],
        "source": "bangumi",
        "trusted_sites": ["bangumi"],
        "strategy": "fill_missing",
    })

    assert result["status"] == "done"
    assert result["processed"] == 1
    args, kwargs = batch.await_args
    assert args[0] == [{"id": "w1", "content_type": "tv"}]
    assert args[1] == "bangumi"
    assert kwargs["trusted_sites"] == ["bangumi"]
    assert kwargs["strategy"] == "fill_missing"


# ---------------------------------------------------------------------------
# _refresh_works_batch (shared per-work loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_maps_strategy_to_only_missing(monkeypatch, db_session):
    _patch_env(monkeypatch, db_session)
    work = TVSeries(id=_uuid(), title_en="Show", content_type="tv", season_number=3)
    db_session.add(work)
    await db_session.flush()
    refresh = AsyncMock(return_value={"found": True, "applied": [], "message": "matched"})
    # The batch loop imports the service lazily — patch at the source module.
    monkeypatch.setattr("app.services.metadata_search.refresh_work_by_source", refresh)

    await _refresh_works_batch(
        [{"id": work.id, "content_type": "tv"}], "bangumi", strategy="fill_missing",
    )
    assert refresh.await_args.kwargs["only_missing"] is True

    await _refresh_works_batch(
        [{"id": work.id, "content_type": "tv"}], "bangumi", strategy="sync_non_manual",
    )
    assert refresh.await_args.kwargs["only_missing"] is False


@pytest.mark.asyncio
async def test_batch_reports_missing_work(monkeypatch, db_session):
    _patch_env(monkeypatch, db_session)
    refresh = AsyncMock()
    monkeypatch.setattr("app.services.metadata_search.refresh_work_by_source", refresh)

    results = await _refresh_works_batch(
        [{"id": "nonexistent", "content_type": "tv"}], "bangumi",
    )
    assert results[0]["found"] is False
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_isolates_per_work_errors(monkeypatch, db_session):
    _patch_env(monkeypatch, db_session)
    good = TVSeries(id=_uuid(), title_en="Good", content_type="tv")
    bad = TVSeries(id=_uuid(), title_en="Bad", content_type="tv")
    db_session.add_all([good, bad])
    await db_session.flush()

    async def _refresh(_session, work, *_args, **_kwargs):
        if work.id == bad.id:
            raise RuntimeError("boom")
        return {"found": True, "applied": [], "message": "matched"}

    monkeypatch.setattr("app.services.metadata_search.refresh_work_by_source", _refresh)
    results = await _refresh_works_batch(
        [{"id": bad.id, "content_type": "tv"}, {"id": good.id, "content_type": "tv"}],
        "bangumi",
    )
    assert results[0]["found"] is False and "boom" in results[0]["error"]
    assert results[1]["found"] is True


@pytest.mark.asyncio
async def test_batch_timeout_does_not_stall_remaining_works(monkeypatch, db_session):
    _patch_env(monkeypatch, db_session)
    slow = TVSeries(id=_uuid(), title_en="Slow", content_type="tv")
    fast = TVSeries(id=_uuid(), title_en="Fast", content_type="tv")
    db_session.add_all([slow, fast])
    await db_session.flush()

    async def _refresh(_session, work, *_args, **_kwargs):
        if work.id == slow.id:
            # Never-completing wait (the unit conftest fast-forwards long
            # asyncio.sleep calls, so a plain sleep cannot simulate a hang).
            await asyncio.Event().wait()
        return {"found": True, "applied": [], "message": "matched"}

    monkeypatch.setattr("app.services.metadata_search.refresh_work_by_source", _refresh)
    monkeypatch.setattr("app.job_handlers._REFRESH_WORK_TIMEOUT", 0.05)
    results = await _refresh_works_batch(
        [{"id": slow.id, "content_type": "tv"}, {"id": fast.id, "content_type": "tv"}],
        "bangumi",
    )
    assert results[0]["found"] is False and results[0]["error"] == "timeout"
    assert results[1]["found"] is True


# ---------------------------------------------------------------------------
# Whole-job time budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_job_time_budget_skips_remaining(monkeypatch, db_session):
    """Once the whole-job budget is spent, remaining works are recorded as
    skipped instead of refreshed, keeping one result entry per input item."""
    _patch_env(monkeypatch, db_session)
    works = [TVSeries(id=_uuid(), title_en=f"W{i}", content_type="tv") for i in range(3)]
    db_session.add_all(works)
    await db_session.flush()
    refresh = AsyncMock(return_value={"found": True, "applied": [], "message": "matched"})
    monkeypatch.setattr("app.services.metadata_search.refresh_work_by_source", refresh)

    # Clock: batch start (deadline = 10), before item 1 (still in budget),
    # before item 2 (budget exhausted).
    clock = iter([0.0, 1.0, 100.0])
    monkeypatch.setattr("app.job_handlers._monotonic", lambda: next(clock))

    results = await _refresh_works_batch(
        [{"id": w.id, "content_type": "tv"} for w in works],
        "bangumi",
        time_budget=10,
    )

    assert len(results) == 3
    assert results[0]["found"] is True
    assert results[1]["skipped"] is True
    assert results[1]["error"] == "job_time_budget_exceeded"
    assert results[1]["id"] == works[1].id
    assert results[2]["skipped"] is True
    assert refresh.await_count == 1


@pytest.mark.asyncio
async def test_batch_zero_time_budget_disables_limit(monkeypatch, db_session):
    _patch_env(monkeypatch, db_session)
    works = [TVSeries(id=_uuid(), title_en=f"W{i}", content_type="tv") for i in range(2)]
    db_session.add_all(works)
    await db_session.flush()
    refresh = AsyncMock(return_value={"found": True, "applied": [], "message": "matched"})
    monkeypatch.setattr("app.services.metadata_search.refresh_work_by_source", refresh)
    # The clock must not be consulted at all when the budget is disabled.
    monkeypatch.setattr(
        "app.job_handlers._monotonic",
        lambda: pytest.fail("disabled budget must not read the clock"),
    )

    results = await _refresh_works_batch(
        [{"id": w.id, "content_type": "tv"} for w in works],
        "bangumi",
        time_budget=0,
    )
    assert [r["found"] for r in results] == [True, True]
    assert refresh.await_count == 2


@pytest.mark.asyncio
async def test_handle_refresh_works_reports_truncation(monkeypatch, db_session):
    """A partially processed batch surfaces processed/total/truncated while
    keeping the existing status/results keys."""
    _patch_env(monkeypatch, db_session)
    batch = AsyncMock(return_value=[
        {"id": "w1", "content_type": "tv", "found": True},
        {"id": "w2", "content_type": "tv", "found": False,
         "skipped": True, "error": "job_time_budget_exceeded"},
    ])
    monkeypatch.setattr("app.job_handlers._refresh_works_batch", batch)

    result = await _handle_refresh_works_metadata({
        "items": [{"id": "w1", "content_type": "tv"}, {"id": "w2", "content_type": "tv"}],
        "source": "bangumi",
    })

    assert result["status"] == "done"
    assert result["processed"] == 1
    assert result["total"] == 2
    assert result["truncated"] is True
    assert len(result["results"]) == 2


@pytest.mark.asyncio
async def test_refresh_batch_summary_ignores_field_level_skipped_lists():
    """``refresh_work_by_source`` returns ``skipped`` as a list of field names
    on success; only the boolean budget marker counts as unprocessed."""
    from app.job_handlers import _refresh_batch_summary

    summary = _refresh_batch_summary(
        [
            {"id": "w1", "found": True, "applied": ["rating"], "skipped": ["poster_url"]},
            {"id": "w2", "found": True},
            {"id": "w3", "found": False, "skipped": True,
             "error": "job_time_budget_exceeded"},
        ],
        total=3,
    )
    assert summary == {"processed": 2, "total": 3, "truncated": True}
