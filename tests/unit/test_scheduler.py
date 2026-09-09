"""Unit tests for scheduler helpers: _sync_download_progress and _cleanup_expired.

We directly exercise the inner logic by constructing a test DB session and
monkey-patching ``app.database.async_session_factory`` so the scheduler helpers
open sessions against the test engine. APScheduler wiring (init/shutdown/add_job)
is left to integration-level coverage; it is marked with ``# pragma: no cover``
where necessary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.agent import Agent
from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.pending_decision import PendingDecision
from app.services import scheduler as sch


def _uuid():
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


@pytest.fixture
async def _seed(db_session):
    """Create a channel, downloader, agent, and a couple of resources/tasks."""
    ch = Channel(id=_uuid(), name="ch", type="rss_feed", url="https://x/rss",
                 field_mapping=TEST_FIELD_MAPPING,
                 metadata_agent_enabled=False)
    dl = DownloaderInstance(
        id=_uuid(), name="dl", type="transmission",
        url="http://127.0.0.1:9091/transmission/rpc",
        download_dir="/downloads/rssripple",
        status="disconnected",
    )
    db_session.add_all([ch, dl])
    await db_session.flush()
    agent = Agent(
        id=_uuid(), name="a", channel_id=ch.id, downloader_id=dl.id,
        scope_channel_wide=True, status="active", task_expire_days=30,
    )
    db_session.add(agent)
    await db_session.flush()

    r1 = FileResource(
        id=_uuid(), channel_id=ch.id, guid="g1", title_raw="T1",
        torrent_url="magnet:?xt=urn:btih:a", search_title="T1",
    )
    r2 = FileResource(
        id=_uuid(), channel_id=ch.id, guid="g2", title_raw="T2",
        torrent_url="magnet:?xt=urn:btih:b", search_title="T2",
    )
    db_session.add_all([r1, r2])
    await db_session.flush()

    t1 = DownloadTask(
        id=_uuid(), agent_id=agent.id, file_resource_id=r1.id, downloader_id=dl.id,
        download_dir="/downloads/rssripple",
        transmission_torrent_id=42, status="downloading", progress=0.1,
    )
    t2 = DownloadTask(
        id=_uuid(), agent_id=agent.id, file_resource_id=r2.id, downloader_id=dl.id,
        download_dir="/downloads/rssripple",
        transmission_torrent_id=43, status="downloading", progress=0.2,
    )
    t_done = DownloadTask(
        id=_uuid(), agent_id=agent.id, file_resource_id=r1.id, downloader_id=dl.id,
        download_dir="/downloads/rssripple",
        transmission_torrent_id=99, status="completed", progress=1.0,
        completed_at=datetime.now(UTC) - timedelta(days=60),
    )
    db_session.add_all([t1, t2, t_done])
    await db_session.commit()
    return SimpleNamespace(ch=ch, dl=dl, agent=agent, r1=r1, r2=r2, t1=t1, t2=t2, t_done=t_done)


@pytest.mark.asyncio
async def test_sync_download_progress_marks_completed_and_paused(db_session, _seed, monkeypatch):
    # Patch async_session_factory in scheduler module to use test session
    class _Ctx:
        async def __aenter__(self_inner):
            return db_session
        async def __aexit__(self_inner, *a):
            await db_session.commit()
            return False
    factory = MagicMock(return_value=_Ctx())
    monkeypatch.setattr(sch, "async_session_factory", factory, raising=False)
    # Also need to make sure sch imports it correctly. Check source: it does `from app.database import async_session_factory` at call time.
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    torrents = [
        {"id": 42, "percent_done": 1.0, "rate_download": 0, "rate_upload": 0,
         "eta_seconds": 0, "is_finished": True, "left_until_done": 0, "status": "stopped"},
        {"id": 43, "percent_done": 0.5, "rate_download": 1024, "rate_upload": 0,
         "eta_seconds": 10, "is_finished": False, "left_until_done": 50, "status": "downloading"},
    ]
    wrapper = MagicMock()
    wrapper.list_torrents = AsyncMock(return_value=torrents)
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        await sch._sync_download_progress()

    # t1 should be completed, t2 should still be downloading
    await db_session.refresh(_seed.t1)
    await db_session.refresh(_seed.t2)
    await db_session.refresh(_seed.dl)
    assert _seed.t1.status == "completed"
    assert _seed.t1.completed_at is not None
    assert _seed.t2.status == "downloading"
    assert _seed.t2.progress == 0.5
    assert _seed.dl.status == "connected"


@pytest.mark.asyncio
async def test_sync_download_progress_marks_cancelled_when_torrent_missing(db_session, _seed, monkeypatch):
    class _Ctx:
        async def __aenter__(self_inner):
            return db_session
        async def __aexit__(self_inner, *a):
            await db_session.commit()
            return False
    factory = MagicMock(return_value=_Ctx())
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    # Only return torrent for t1 — t2 is missing
    wrapper = MagicMock()
    wrapper.list_torrents = AsyncMock(return_value=[{"id": 42, "percent_done": 0.3,
        "rate_download": 1, "rate_upload": 0, "eta_seconds": 5,
        "is_finished": False, "left_until_done": 10, "status": "downloading"}])
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        await sch._sync_download_progress()

    await db_session.refresh(_seed.t2)
    assert _seed.t2.status == "cancelled"


@pytest.mark.asyncio
async def test_sync_download_progress_rpc_failure_keeps_task_status(db_session, _seed, monkeypatch):
    """A transient RPC failure must not cascade error onto tasks: only the
    downloader is flagged; tasks keep their last-known status."""
    class _Ctx:
        async def __aenter__(self_inner):
            return db_session
        async def __aexit__(self_inner, *a):
            await db_session.commit()
            return False
    factory = MagicMock(return_value=_Ctx())
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    wrapper = MagicMock()
    wrapper.list_torrents = AsyncMock(side_effect=Exception("conn refused"))
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        await sch._sync_download_progress()

    await db_session.refresh(_seed.t1)
    await db_session.refresh(_seed.t2)
    await db_session.refresh(_seed.dl)
    assert _seed.t1.status == "downloading"
    assert _seed.t2.status == "downloading"
    assert _seed.t1.error_message is None
    assert _seed.dl.status == "error"


@pytest.mark.asyncio
async def test_sync_download_progress_self_heals_outage_error_tasks(db_session, _seed, monkeypatch):
    """Error tasks caused by a past outage cascade (message prefix
    'Transmission unreachable', torrent still in the daemon) are picked up
    by the sync again and resume normal tracking."""
    class _Ctx:
        async def __aenter__(self_inner):
            return db_session
        async def __aexit__(self_inner, *a):
            await db_session.commit()
            return False
    factory = MagicMock(return_value=_Ctx())
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    # t1: outage-cascade error, torrent alive and finished -> completed.
    _seed.t1.status = "error"
    _seed.t1.error_message = "Transmission unreachable: timeout"
    # t2: outage-cascade error, torrent alive and downloading -> downloading.
    _seed.t2.status = "error"
    _seed.t2.error_message = "Transmission unreachable: timeout"
    # t_err: genuine dispatch error (no cascade prefix) -> untouched.
    t_err = DownloadTask(
        id=_uuid(), agent_id=_seed.agent.id, file_resource_id=_seed.r1.id,
        downloader_id=_seed.dl.id, download_dir="/downloads/rssripple",
        transmission_torrent_id=77, status="error", progress=0.0,
        error_message="magnet parse failed",
    )
    db_session.add(t_err)
    await db_session.commit()

    torrents = [
        {"id": 42, "percent_done": 1.0, "rate_download": 0, "rate_upload": 0,
         "eta_seconds": 0, "is_finished": True, "left_until_done": 0,
         "total_size": 100, "status": "stopped"},
        {"id": 43, "percent_done": 0.5, "rate_download": 1024, "rate_upload": 0,
         "eta_seconds": 10, "is_finished": False, "left_until_done": 50,
         "total_size": 100, "status": "downloading"},
    ]
    wrapper = MagicMock()
    wrapper.list_torrents = AsyncMock(return_value=torrents)
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        await sch._sync_download_progress()

    await db_session.refresh(_seed.t1)
    await db_session.refresh(_seed.t2)
    await db_session.refresh(t_err)
    assert _seed.t1.status == "completed"
    assert _seed.t1.error_message is None
    assert _seed.t2.status == "downloading"
    assert _seed.t2.progress == 0.5
    assert _seed.t2.error_message is None
    # Non-outage error tasks are left alone.
    assert t_err.status == "error"
    assert t_err.error_message == "magnet parse failed"


@pytest.mark.asyncio
async def test_cleanup_expired_expires_decisions_and_deletes_tasks(db_session, _seed, monkeypatch):
    class _Ctx:
        async def __aenter__(self_inner):
            return db_session
        async def __aexit__(self_inner, *a):
            await db_session.commit()
            return False
    factory = MagicMock(return_value=_Ctx())
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    # Expired pending decision
    pd = PendingDecision(
        id=_uuid(), agent_id=_seed.agent.id, status="pending",
        candidates=[_seed.r1.id, _seed.r2.id], reason="冲突",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    # Non-expired decision
    pd_active = PendingDecision(
        id=_uuid(), agent_id=_seed.agent.id, status="pending",
        candidates=[_seed.r1.id], reason="冲突2",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add_all([pd, pd_active])
    await db_session.commit()

    await sch._cleanup_expired()

    await db_session.refresh(pd)
    await db_session.refresh(pd_active)
    from sqlalchemy import func, select
    count = (await db_session.execute(
        select(func.count()).select_from(DownloadTask).where(DownloadTask.id == _seed.t_done.id)
    )).scalar_one()
    assert pd.status == "expired"
    assert pd_active.status == "pending"
    assert count == 0


@pytest.mark.asyncio
async def test_check_downloader_connections_marks_status(db_session, _seed, monkeypatch):
    class _Ctx:
        async def __aenter__(self_inner): return db_session
        async def __aexit__(self_inner, *a):
            await db_session.commit()
            return False
    factory = MagicMock(return_value=_Ctx())
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    wrapper = MagicMock()
    wrapper.test_connection = AsyncMock(return_value=(True, "ok"))
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        await sch._check_downloader_connections()
    await db_session.refresh(_seed.dl)
    assert _seed.dl.status == "connected"


@pytest.mark.asyncio
async def test_check_downloader_connections_failure(db_session, _seed, monkeypatch):
    class _Ctx:
        async def __aenter__(self_inner): return db_session
        async def __aexit__(self_inner, *a):
            await db_session.commit()
            return False
    factory = MagicMock(return_value=_Ctx())
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    wrapper = MagicMock()
    wrapper.test_connection = AsyncMock(side_effect=RuntimeError("down"))
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        await sch._check_downloader_connections()
    await db_session.refresh(_seed.dl)
    assert _seed.dl.status == "error"


# ---------------------------------------------------------------------------
# Periodic-job enqueue wrappers: the scheduler only enqueues (stable key);
# the function bodies are executed by the queue consumer.
# ---------------------------------------------------------------------------

_PERIODIC_WRAPPERS = [
    ("_enqueue_sync_progress", "sync_progress"),
    ("_enqueue_daily_cleanup", "daily_cleanup"),
    ("_enqueue_daily_dedup", "daily_dedup"),
    ("_enqueue_check_downloaders", "check_downloaders"),
    ("_enqueue_fts_drain", "fts_drain"),
    ("_enqueue_fts_reconcile", "fts_reconcile"),
    ("_enqueue_download_notifications", "download_notifications"),
    ("_enqueue_magnet_resolve_sweep", "magnet_resolve_sweep"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper_name,job_type", _PERIODIC_WRAPPERS)
async def test_periodic_job_wrapper_enqueues_with_stable_key(monkeypatch, wrapper_name, job_type):
    import app.services.task_queue as tq_mod

    fake_queue = MagicMock()
    fake_queue.throttle = AsyncMock(return_value=True)
    fake_queue.enqueue = AsyncMock(return_value={"status": "queued"})
    monkeypatch.setattr(tq_mod, "task_queue", fake_queue)

    await getattr(sch, wrapper_name)()

    fake_queue.throttle.assert_awaited_once()
    fake_queue.enqueue.assert_awaited_once_with(job_type, f"job:{job_type}", {})


@pytest.mark.asyncio
async def test_periodic_job_wrapper_skips_when_throttled(monkeypatch):
    """A losing tick (another worker already ticked this interval) must not
    enqueue at all — this is what keeps N schedulers to one job/interval."""
    import app.services.task_queue as tq_mod

    fake_queue = MagicMock()
    fake_queue.throttle = AsyncMock(return_value=False)
    fake_queue.enqueue = AsyncMock()
    monkeypatch.setattr(tq_mod, "task_queue", fake_queue)

    await sch._enqueue_sync_progress()

    fake_queue.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_periodic_job_wrapper_swallows_enqueue_errors(monkeypatch):
    """A queue outage must not crash the scheduler job — just log and drop."""
    import app.services.task_queue as tq_mod

    fake_queue = MagicMock()
    fake_queue.throttle = AsyncMock(return_value=True)
    fake_queue.enqueue = AsyncMock(side_effect=ConnectionError("redis down"))
    monkeypatch.setattr(tq_mod, "task_queue", fake_queue)

    await sch._enqueue_sync_progress()  # must not raise


# ---------------------------------------------------------------------------
# _sync_download_progress: skip branches + status mapping
# ---------------------------------------------------------------------------


def _ctx_factory(db_session):
    class _Ctx:
        async def __aenter__(self_inner):
            return db_session

        async def __aexit__(self_inner, *a):
            await db_session.commit()
            return False

    factory = MagicMock(return_value=_Ctx())

    return factory


@pytest.mark.asyncio
async def test_sync_download_progress_skips_missing_downloader(
    db_session, _seed, monkeypatch
):
    """A downloader row that no longer exists is skipped (no crash)."""
    t = DownloadTask(
        id=_uuid(), agent_id=_seed.agent.id, file_resource_id=_seed.r1.id,
        downloader_id=_seed.dl.id, download_dir="/downloads/rssripple",
        transmission_torrent_id=56, status="downloading", progress=0.0,
    )
    db_session.add(t)
    await db_session.commit()

    # The session's DownloaderInstance.get returns None (row vanished).
    real_get = db_session.get

    async def _ghost_get(model, pk, *a, **kw):
        if model is DownloaderInstance:
            return None
        return await real_get(model, pk, *a, **kw)

    class _Ctx:
        async def __aenter__(self_inner):
            return db_session

        async def __aexit__(self_inner, *a):
            await db_session.commit()
            return False

    db_session.get = _ghost_get  # type: ignore[method-assign]
    factory = MagicMock(return_value=_Ctx())
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)
    try:
        wrapper = MagicMock()
        wrapper.list_torrents = AsyncMock(return_value=[])
        with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
            await sch._sync_download_progress()
    finally:
        db_session.get = real_get  # type: ignore[method-assign]

    await db_session.refresh(t)
    assert t.status == "downloading"


@pytest.mark.asyncio
async def test_sync_download_progress_paused_and_queued_statuses(
    db_session, _seed, monkeypatch
):
    """'stopped' torrents map to paused; queued/downloading mapping too."""
    t_pause = DownloadTask(
        id=_uuid(), agent_id=_seed.agent.id, file_resource_id=_seed.r1.id,
        downloader_id=_seed.dl.id, download_dir="/downloads/rssripple",
        transmission_torrent_id=70, status="downloading", progress=0.1,
    )
    t_queued = DownloadTask(
        id=_uuid(), agent_id=_seed.agent.id, file_resource_id=_seed.r2.id,
        downloader_id=_seed.dl.id, download_dir="/downloads/rssripple",
        transmission_torrent_id=71, status="downloading", progress=0.1,
    )
    t_dl = DownloadTask(
        id=_uuid(), agent_id=_seed.agent.id, file_resource_id=_seed.r1.id,
        downloader_id=_seed.dl.id, download_dir="/downloads/rssripple",
        transmission_torrent_id=72, status="downloading", progress=0.1,
    )
    db_session.add_all([t_pause, t_queued, t_dl])
    await db_session.commit()

    factory = _ctx_factory(db_session)
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    torrents = [
        {"id": 70, "percent_done": 0.3, "rate_download": 0, "rate_upload": 0,
         "eta_seconds": 5, "is_finished": False, "left_until_done": 10,
         "total_size": 100, "status": "stopped"},
        {"id": 71, "percent_done": 0.3, "rate_download": 0, "rate_upload": 0,
         "eta_seconds": 5, "is_finished": False, "left_until_done": 10,
         "total_size": 100, "status": "queued"},
        {"id": 72, "percent_done": 0.3, "rate_download": 2048, "rate_upload": 0,
         "eta_seconds": 5, "is_finished": False, "left_until_done": 10,
         "total_size": 100, "status": "unknown-status"},
    ]
    wrapper = MagicMock()
    wrapper.list_torrents = AsyncMock(return_value=torrents)
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        await sch._sync_download_progress()

    await db_session.refresh(t_pause)
    await db_session.refresh(t_queued)
    await db_session.refresh(t_dl)
    assert t_pause.status == "paused"
    assert t_queued.status == "queued"
    assert t_dl.status == "downloading"


# ---------------------------------------------------------------------------
# _cleanup_expired: resource-cleanup report + exception branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_expired_reports_and_handles_cleanup_failure(
    db_session, _seed, monkeypatch
):
    factory = _ctx_factory(db_session)
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    with patch(
        "app.services.resource_cleanup.cleanup_stale_unresolved_resources",
        new=AsyncMock(return_value={"deleted": 3, "channels": 1}),
    ):
        await sch._cleanup_expired()

    with patch(
        "app.services.resource_cleanup.cleanup_stale_unresolved_resources",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await sch._cleanup_expired()  # must not raise


# ---------------------------------------------------------------------------
# _dedup_metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_metadata_commits_report(db_session, monkeypatch):
    class _Report:
        series_removed = 2
        movies_removed = 1

    factory = _ctx_factory(db_session)
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    with patch(
        "app.services.metadata_dedup.merge_duplicate_metadata",
        new=AsyncMock(return_value=_Report()),
    ):
        await sch._dedup_metadata()

    with patch(
        "app.services.metadata_dedup.merge_duplicate_metadata",
        new=AsyncMock(side_effect=RuntimeError("dedup failed")),
    ):
        await sch._dedup_metadata()  # rollback + log, no raise


# ---------------------------------------------------------------------------
# _process_download_notifications: exception branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_download_notifications_disabled(db_session, monkeypatch):
    import app.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "settings", SimpleNamespace(notify_enabled=False))
    await sch._process_download_notifications()  # early return


@pytest.mark.asyncio
async def test_process_download_notifications_tick_failure_swallowed(
    db_session, monkeypatch
):
    import app.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "settings", SimpleNamespace(notify_enabled=True))
    factory = _ctx_factory(db_session)
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    with patch(
        "app.services.notify_service.create_notification_for_task",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await sch._process_download_notifications()  # must not raise


@pytest.mark.asyncio
async def test_process_download_notifications_full_tick(
    db_session, _seed, monkeypatch
):
    """A completed task with an enabled webhook flows through the whole tick:
    notification creation, organize planning, fan-out and delivery."""
    import app.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "settings", SimpleNamespace(notify_enabled=True))
    factory = _ctx_factory(db_session)
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    from app.models.agent_webhook import AgentWebhook
    from app.models.download_notification import DownloadNotification

    webhook = AgentWebhook(
        id=_uuid(), agent_id=_seed.agent.id, url="http://example.com/hook",
        enabled=True,
    )
    db_session.add(webhook)
    _seed.t_done.status = "completed"
    _seed.t_done.completed_at = datetime.now(UTC)
    await db_session.commit()

    notif = DownloadNotification(
        id=_uuid(), agent_id=_seed.agent.id,
        download_task_id=_seed.t_done.id,
        payload={"version": 2, "task_id": _seed.t_done.id},
    )

    async def _create_notification(db, task):
        return notif, True

    async def _plan_for_notifications(db, targets):
        return {"planned": 1, "rebuilt": 0, "uncategorized": 0,
                "skipped": 0, "failed": 0}

    with patch(
        "app.services.notify_service.create_notification_for_task",
        new=AsyncMock(side_effect=_create_notification),
    ), patch(
        "app.services.organize_service.plan_for_notifications",
        new=AsyncMock(side_effect=_plan_for_notifications),
    ), patch(
        "app.services.notify_service.ensure_deliveries",
        new=AsyncMock(return_value=2),
    ), patch(
        "app.services.notify_service.deliver_due_deliveries",
        new=AsyncMock(return_value={"delivered": 1, "failed": 0, "skipped": 1}),
    ):
        await sch._process_download_notifications()


@pytest.mark.asyncio
async def test_process_download_notifications_orphan_scan_failure(
    db_session, _seed, monkeypatch
):
    """The organize orphan back-scan failure is non-fatal (logged only)."""
    import app.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "settings", SimpleNamespace(notify_enabled=True))
    factory = _ctx_factory(db_session)
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    from app.models.agent_webhook import AgentWebhook

    webhook = AgentWebhook(
        id=_uuid(), agent_id=_seed.agent.id, url="http://example.com/hook",
        enabled=True,
    )
    db_session.add(webhook)
    await db_session.commit()

    async def _create_notification(db, task):
        return None, False

    # Patch only the ORPHAN query (the second SELECT of DownloadNotification
    # inside the try block) to raise; the task query must still succeed.
    from sqlalchemy import select

    from app.models.download_notification import DownloadNotification

    real_execute = db_session.execute
    call_count = 0

    async def _selective_fail_execute(stmt, *a, **kw):
        nonlocal call_count
        # First select on DownloadNotification is the `notified` subquery in
        # the task stmt; failing it would break the whole tick. Only fail the
        # orphan back-scan (second standalone DownloadNotification select that
        # has no join and no `status` filter).
        if isinstance(stmt, select) and stmt.whereclause is not None:

            has_task_table = any(
                col.table.key == "download_tasks" for col in stmt.whereclause.get_children(
                    **{"iterate": True}
                ) if hasattr(col, "table")
            )
            if not has_task_table and any(
                isinstance(desc, DownloadNotification)
                for desc in getattr(stmt, "_entities", [])
            ):
                raise RuntimeError("orphan scan failed")
        return await real_execute(stmt, *a, **kw)

    db_session.execute = _selective_fail_execute  # type: ignore[method-assign]
    try:
        with patch(
            "app.services.notify_service.create_notification_for_task",
            new=AsyncMock(side_effect=_create_notification),
        ), patch(
            "app.services.notify_service.ensure_deliveries",
            new=AsyncMock(return_value=0),
        ), patch(
            "app.services.notify_service.deliver_due_deliveries",
            new=AsyncMock(return_value={"delivered": 0, "failed": 0, "skipped": 0}),
        ):
            await sch._process_download_notifications()  # must not raise
    finally:
        db_session.execute = real_execute  # type: ignore[method-assign]


def test_get_scheduler_uninitialized_raises():
    saved = sch._scheduler
    sch._scheduler = None
    try:
        with pytest.raises(RuntimeError, match="not initialized"):
            sch.get_scheduler()
    finally:
        sch._scheduler = saved


@pytest.mark.asyncio
async def test_cleanup_expired_deletes_old_notifications(
    db_session, _seed, monkeypatch
):
    """Notifications older than the retention window are deleted."""
    from app.models.download_notification import DownloadNotification

    old_notif = DownloadNotification(
        id=_uuid(), agent_id=_seed.agent.id,
        download_task_id=_seed.t_done.id, payload={},
    )
    old_notif.created_at = datetime.now(UTC) - timedelta(days=60)
    db_session.add(old_notif)
    await db_session.commit()

    factory = _ctx_factory(db_session)
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)
    with patch(
        "app.services.resource_cleanup.cleanup_stale_unresolved_resources",
        new=AsyncMock(return_value={"deleted": 0, "channels": 0}),
    ):
        await sch._cleanup_expired()

    from sqlalchemy import func, select

    count = (await db_session.execute(
        select(func.count()).select_from(DownloadNotification)
    )).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# _drain_fts_outbox / _reconcile_fts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_fts_outbox_success_and_failure(db_session, monkeypatch):
    factory = _ctx_factory(db_session)
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    with patch("app.services.fts.drain_fts_outbox", new=AsyncMock(return_value=5)):
        await sch._drain_fts_outbox()

    with patch("app.services.fts.drain_fts_outbox",
               new=AsyncMock(side_effect=RuntimeError("fts down"))):
        await sch._drain_fts_outbox()  # logged, not raised


@pytest.mark.asyncio
async def test_reconcile_fts_success_and_failure(db_session, monkeypatch):
    factory = _ctx_factory(db_session)
    import app.database as dbmod
    monkeypatch.setattr(dbmod, "async_session_factory", factory)

    with patch("app.services.fts.reconcile_fts",
               new=AsyncMock(return_value={"updated": 2, "deleted": 1})):
        await sch._reconcile_fts()

    with patch("app.services.fts.reconcile_fts",
               new=AsyncMock(side_effect=RuntimeError("fts down"))):
        await sch._reconcile_fts()  # logged, not raised
