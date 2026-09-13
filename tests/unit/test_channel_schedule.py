"""Three independent scheduler instances converge without resetting deadlines."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.services import scheduler as sch
from app.services.channel_schedule import reconcile_channel_jobs


async def fetch(channel_id):
    pass


async def refresh(channel_id):
    pass


def channel(id="one", **values):
    return SimpleNamespace(**{
        "id": id, "status": "active", "fetch_interval": 1800,
        "metadata_refresh_enabled": True, "metadata_refresh_interval_minutes": 60,
        **values,
    })


def database(rows):
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
    return db


async def reconcile(db, scheduler):
    return await reconcile_channel_jobs(db, scheduler, fetch_callback=fetch, refresh_callback=refresh)


@pytest.fixture
async def schedulers():
    instances = [AsyncIOScheduler() for _ in range(3)]
    for scheduler in instances:
        scheduler.start(paused=True)
    yield instances
    for scheduler in instances:
        scheduler.shutdown(wait=False)


async def test_all_workers_converge_across_create_edit_pause_resume_delete(schedulers):
    row = channel()
    rows = [row]
    db = database(rows)
    for scheduler in schedulers:
        assert (await reconcile(db, scheduler))["added"] == 2
        before = {job.id: job.next_run_time for job in scheduler.get_jobs()}
        assert not any((await reconcile(db, scheduler)).values())
        assert {job.id: job.next_run_time for job in scheduler.get_jobs()} == before

    row.fetch_interval = 600
    for scheduler in schedulers:
        before = scheduler.get_job("channel-refresh:one").next_run_time
        assert (await reconcile(db, scheduler))["updated"] == 1
        assert scheduler.get_job("channel:one").trigger.interval.total_seconds() == 600
        assert scheduler.get_job("channel-refresh:one").next_run_time == before

    row.status = "error"  # transient feed failure must not restart the timer
    for scheduler in schedulers:
        assert not any((await reconcile(db, scheduler)).values())
    row.metadata_refresh_enabled = False
    for scheduler in schedulers:
        assert (await reconcile(db, scheduler))["removed"] == 1
        assert scheduler.get_job("channel:one") is not None
    row.metadata_refresh_enabled = True
    row.metadata_refresh_interval_minutes = 120
    for scheduler in schedulers:
        assert (await reconcile(db, scheduler))["added"] == 1
        assert scheduler.get_job("channel-refresh:one").trigger.interval.total_seconds() == 7200
    row.status = "inactive"
    for scheduler in schedulers:
        assert (await reconcile(db, scheduler))["removed"] == 2
    row.status = "active"
    for scheduler in schedulers:
        assert (await reconcile(db, scheduler))["added"] == 2
    rows.clear()
    for scheduler in schedulers:
        assert (await reconcile(db, scheduler))["removed"] == 2
        assert not scheduler.get_jobs()


async def test_reconcile_queries_only_columns_and_keeps_unrelated_jobs(schedulers):
    scheduler = schedulers[0]
    scheduler.add_job(fetch, "interval", seconds=60, id="sync_progress", args=["unused"])
    db = database([channel(metadata_refresh_enabled=False)])
    await reconcile(db, scheduler)
    assert scheduler.get_job("sync_progress") is not None
    statement = db.execute.call_args.args[0]
    assert set(statement.selected_columns.keys()) == {
        "id", "status", "fetch_interval", "metadata_refresh_enabled", "metadata_refresh_interval_minutes",
    }


async def test_one_invalid_channel_does_not_remove_existing_or_block_others(schedulers):
    scheduler = schedulers[0]
    row = channel()
    await reconcile(database([row]), scheduler)
    deadline = scheduler.get_job("channel:one").next_run_time
    row.fetch_interval = 0
    stats = await reconcile(database([row, channel("two")]), scheduler)
    assert stats == {"added": 2, "updated": 0, "removed": 0, "errors": 1}
    assert scheduler.get_job("channel:one").next_run_time == deadline
    assert scheduler.get_job("channel:two") is not None


async def test_missing_individual_job_is_restored(schedulers):
    scheduler = schedulers[0]
    db = database([channel()])
    await reconcile(db, scheduler)
    scheduler.remove_job("channel:one")
    assert (await reconcile(db, scheduler))["added"] == 1


async def test_database_outage_preserves_schedules_then_recovers(schedulers, monkeypatch, caplog):
    import app.database as db_module
    from app.config import settings

    scheduler = schedulers[0]
    db = database([channel()])
    monkeypatch.setattr(sch, "_scheduler", scheduler)
    monkeypatch.setattr(settings, "scheduler_enabled", True)

    class Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(db_module, "async_session_factory", Session)
    await sch._reconcile_channel_schedules()
    before = {job.id: job.next_run_time for job in scheduler.get_jobs()}
    db.execute.side_effect = OSError("database unavailable")
    await sch._reconcile_channel_schedules()
    assert "retrying next tick" in caplog.text
    assert {job.id: job.next_run_time for job in scheduler.get_jobs()} == before
    db.execute.side_effect = None
    db.execute.return_value = SimpleNamespace(all=lambda: [])
    await sch._reconcile_channel_schedules()
    assert not scheduler.get_jobs()


async def test_disabled_scheduler_does_not_read_database(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "scheduler_enabled", False)
    db = database([])
    await sch.setup_channel_jobs(db)
    db.execute.assert_not_called()


async def test_periodic_reconciliation_registered_locally_with_grace(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://unused")
    monkeypatch.setattr(sch, "_scheduler", None)
    await sch.init_scheduler()
    try:
        job = sch.get_scheduler().get_job("channel_schedule_reconcile")
        assert job.func == sch._reconcile_channel_schedules
        assert job.trigger.interval.total_seconds() == 30
        assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time == 30
    finally:
        await sch.shutdown_scheduler()


@pytest.mark.parametrize("callback,job_type", [
    (sch._run_channel_fetch, "fetch_channel"),
    (sch._run_channel_works_refresh, "refresh_channel_works"),
])
async def test_scheduled_payload_is_distinct_from_manual(callback, job_type, monkeypatch):
    from app.services import task_queue as module

    queue = SimpleNamespace(enqueue=AsyncMock())
    monkeypatch.setattr(module, "task_queue", queue)
    await callback("one")
    assert queue.enqueue.call_args.args[0] == job_type
    assert queue.enqueue.call_args.args[2] == {"channel_id": "one", "scheduled": True}


@pytest.mark.parametrize("state", [None, "inactive"])
async def test_stale_scheduled_fetch_skips_without_external_io(state, monkeypatch):
    from app import job_handlers
    from app.services import fetch_service

    db = SimpleNamespace(get=AsyncMock(return_value=channel(status=state) if state else None))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(job_handlers, "committed_session", lambda: context)
    monkeypatch.setattr(job_handlers, "_refresh_runtime_config", AsyncMock())
    fetcher = AsyncMock()
    monkeypatch.setattr(fetch_service, "fetch_channel_resources", fetcher)
    result = await job_handlers._handle_fetch_channel({"channel_id": "one", "scheduled": True})
    assert result["status"] == "skipped"
    fetcher.assert_not_called()


async def test_manual_fetch_of_paused_channel_remains_available(monkeypatch):
    from app import job_handlers
    from app.services import fetch_service

    row = channel(status="inactive")
    db = SimpleNamespace(get=AsyncMock(return_value=row))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(job_handlers, "committed_session", lambda: context)
    monkeypatch.setattr(job_handlers, "_refresh_runtime_config", AsyncMock())
    fetcher = AsyncMock(return_value={"status": "success"})
    monkeypatch.setattr(fetch_service, "fetch_channel_resources", fetcher)
    await job_handlers._handle_fetch_channel({"channel_id": "one", "force": True})
    fetcher.assert_awaited_once_with(row, db, force=True)


async def test_queued_automatic_refresh_rechecks_refresh_switch(monkeypatch):
    from app import job_handlers
    from app.services import metadata_service

    row = channel(metadata_refresh_enabled=False)
    db = SimpleNamespace(get=AsyncMock(return_value=row))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(job_handlers, "committed_session", lambda: context)
    monkeypatch.setattr(job_handlers, "_refresh_runtime_config", AsyncMock())
    select_works = AsyncMock()
    monkeypatch.setattr(metadata_service, "select_channel_works_for_refresh", select_works)
    result = await job_handlers._handle_refresh_channel_works({"channel_id": "one", "scheduled": True})
    assert result["status"] == "skipped"
    select_works.assert_not_called()
