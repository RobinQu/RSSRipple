"""Scheduler periodic-job in-process coverage.

The HTTP suite drives the scheduler only through the app-llm instance's
per-minute notify tick (``_process_download_notifications``, covered by
tests/integration/organize/test_organize_pipeline.py). Here the remaining
periodic tick bodies are exercised directly against the per-test Turso
engine: the periodic enqueuers, download-progress sync (task status
transitions + downloader health), the daily cleanup (expired decisions,
expired completed tasks with the open-notification survival guard, old
notification retention) and the hourly downloader connectivity check.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.agent import Agent
from app.models.agent_webhook import AgentWebhook
from app.models.channel import Channel
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.pending_decision import PendingDecision
from app.models.series import TVSeries
from app.models.webhook_delivery import WebhookDelivery
from app.services import scheduler
from app.utils.time import utcnow


def _uuid() -> str:
    return str(uuid.uuid4())


async def _seed_task(db, *, status="downloading", agent=None, downloader=None,
                     completed_at=None, torrent_id: int | None = 7,
                     downloader_status="disconnected") -> SimpleNamespace:
    channel = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        fetch_interval=1800, status="active",
        field_mapping={"list_locator": {"source": "entries"}},
        metadata_agent_enabled=False,
    )
    dl = downloader or DownloaderInstance(
        id=_uuid(), name="dl", type="transmission",
        url="http://127.0.0.1:9091/transmission/rpc",
        download_dir="/downloads", status=downloader_status,
    )
    ag = agent or Agent(
        id=_uuid(), name="agent", channel_id=channel.id,
        downloader_id=dl.id, task_expire_days=30,
    )
    series = TVSeries(id=_uuid(), title_cn="剧集", title_en="Series", content_type="tv")
    db.add_all([channel, dl, ag, series])
    await db.flush()
    resource = FileResource(
        id=_uuid(), channel_id=channel.id, guid=_uuid(), title_raw="raw",
        torrent_url="magnet:?xt=urn:btih:abc", series_id=series.id,
    )
    task = DownloadTask(
        id=_uuid(), agent_id=ag.id, file_resource_id=resource.id,
        downloader_id=dl.id, download_dir="/downloads",
        transmission_torrent_id=torrent_id, status=status,
        completed_at=completed_at, error_message=None,
    )
    db.add_all([resource, task])
    await db.commit()
    return SimpleNamespace(channel=channel, downloader=dl, agent=ag,
                           resource=resource, task=task)


# ---------------------------------------------------------------------------
# Periodic enqueuers
# ---------------------------------------------------------------------------


async def test_periodic_enqueuers_forward_to_queue(db_session, monkeypatch):
    enqueued: list[tuple[str, str]] = []

    class _FakeQueue:
        async def throttle(self, *a):
            return True

        async def enqueue(self, job_type, key, payload=None):
            enqueued.append((job_type, key))

    monkeypatch.setattr(
        "app.services.task_queue.task_queue", _FakeQueue()
    )
    for fn in (
        scheduler._enqueue_sync_progress,
        scheduler._enqueue_daily_cleanup,
        scheduler._enqueue_daily_dedup,
        scheduler._enqueue_check_downloaders,
        scheduler._enqueue_fts_drain,
        scheduler._enqueue_fts_reconcile,
        scheduler._enqueue_download_notifications,
    ):
        await fn()
    assert {t for t, _ in enqueued} == {
        "sync_progress", "daily_cleanup", "daily_dedup", "check_downloaders",
        "fts_drain", "fts_reconcile", "download_notifications",
    }


async def test_enqueue_periodic_job_throttled_and_error_paths(db_session, monkeypatch):
    class _Throttled:
        async def throttle(self, *a):
            return False

    monkeypatch.setattr("app.services.task_queue.task_queue", _Throttled())
    await scheduler._enqueue_periodic_job("sync_progress")  # returns silently

    class _Exploding:
        async def throttle(self, *a):
            raise RuntimeError("queue down")

    monkeypatch.setattr("app.services.task_queue.task_queue", _Exploding())
    await scheduler._enqueue_periodic_job("sync_progress")  # warning only


# ---------------------------------------------------------------------------
# _sync_download_progress
# ---------------------------------------------------------------------------


def _torrent(tid=7, percent_done=0.5, rate_download=1000, rate_upload=10, is_finished=False,
             status="downloading", left_until_done=500, total_size=1000, eta=None):
    return {
        "id": tid, "percent_done": percent_done, "rate_download": rate_download,
        "rate_upload": rate_upload, "eta_seconds": eta, "is_finished": is_finished,
        "left_until_done": left_until_done, "total_size": total_size, "status": status,
    }


async def test_sync_download_progress_transitions(db_session, session_factory, monkeypatch):
    chain = await _seed_task(db_session, status="downloading")
    chain2 = await _seed_task(db_session, status="downloading", torrent_id=8)
    chain3 = await _seed_task(db_session, status="error", torrent_id=9)
    # A task whose torrent vanished → cancelled.
    chain4 = await _seed_task(db_session, status="downloading", torrent_id=10)

    torrents = [
        _torrent(tid=chain.task.transmission_torrent_id, is_finished=True,
                 left_until_done=0, total_size=1000),
        _torrent(tid=chain2.task.transmission_torrent_id, status="stopped",
                 percent_done=0.9),
        _torrent(tid=chain3.task.transmission_torrent_id, status="downloading",
                 rate_download=0),
    ]
    list_mock = AsyncMock(return_value=torrents)

    monkeypatch.setattr(
        "app.clients.transmission.TransmissionWrapper.list_torrents", list_mock
    )

    async with session_factory() as db:
        # chain3 has a stale outage error (self-heal path).
        fresh = await db.get(DownloadTask, chain3.task.id)
        fresh.error_message = "Transmission unreachable"
        await db.commit()
    await scheduler._sync_download_progress()

    async with session_factory() as db:
        t1 = await db.get(DownloadTask, chain.task.id)
        t2 = await db.get(DownloadTask, chain2.task.id)
        t3 = await db.get(DownloadTask, chain3.task.id)
        t4 = await db.get(DownloadTask, chain4.task.id)
        dl = await db.get(DownloaderInstance, chain.downloader.id)
        assert t1.status == "completed" and t1.completed_at is not None
        assert t1.progress == 0.5
        assert t1.error_message is None
        assert t2.status == "paused"
        assert t3.status == "queued"
        assert t4.status == "cancelled"
        assert dl.status == "connected"
        assert dl.last_checked_at is not None


async def test_sync_download_progress_rpc_failure_flags_downloader(
    db_session, session_factory, monkeypatch
):
    chain = await _seed_task(db_session, status="downloading")
    monkeypatch.setattr(
        "app.clients.transmission.TransmissionWrapper.list_torrents",
        AsyncMock(side_effect=RuntimeError("rpc down")),
    )
    await scheduler._sync_download_progress()
    async with session_factory() as db:
        dl = await db.get(DownloaderInstance, chain.downloader.id)
        assert dl.status == "error"
        assert dl.last_checked_at is not None
        t = await db.get(DownloadTask, chain.task.id)
        assert t.status == "downloading"  # untouched


async def test_sync_download_progress_skips_downloaderless_tasks(
    db_session, session_factory, monkeypatch
):
    # A task without a torrent id is skipped from the RPC round-trip (the
    # statuses stay untouched), and a downloader id that points nowhere is
    # tolerated.
    chain = await _seed_task(db_session, status="downloading", torrent_id=None)
    await scheduler._sync_download_progress()
    async with session_factory() as db:
        t = await db.get(DownloadTask, chain.task.id)
        assert t.status == "downloading"


# ---------------------------------------------------------------------------
# _check_downloader_connections
# ---------------------------------------------------------------------------


async def test_check_downloader_connections(db_session, session_factory, monkeypatch):
    ok_dl = DownloaderInstance(
        id=_uuid(), name="dl-ok", type="transmission",
        url="http://ok/rpc", download_dir="/downloads",
    )
    bad_dl = DownloaderInstance(
        id=_uuid(), name="dl-bad", type="transmission",
        url="http://bad/rpc", download_dir="/downloads",
    )
    db_session.add_all([ok_dl, bad_dl])
    await db_session.commit()

    monkeypatch.setattr(
        "app.clients.transmission.TransmissionWrapper.test_connection",
        AsyncMock(side_effect=[(True, "ok"), (False, "no")]),
    )
    await scheduler._check_downloader_connections()

    async with session_factory() as db:
        ok = await db.get(DownloaderInstance, ok_dl.id)
        bad = await db.get(DownloaderInstance, bad_dl.id)
        assert ok.status == "connected"
        assert bad.status == "error"
        assert ok.last_checked_at is not None


# ---------------------------------------------------------------------------
# _cleanup_expired
# ---------------------------------------------------------------------------


async def test_cleanup_expired_decisions_tasks_and_notifications(
    db_session, session_factory
):
    now = utcnow()
    channel = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        fetch_interval=1800, status="active",
        field_mapping={"list_locator": {"source": "entries"}},
        metadata_agent_enabled=False,
    )
    downloader = DownloaderInstance(
        id=_uuid(), name="dl", type="transmission",
        url="http://127.0.0.1:9091/transmission/rpc",
        download_dir="/downloads", status="disconnected",
    )
    agent = Agent(id=_uuid(), name="agent", channel_id=channel.id,
                  downloader_id=downloader.id, task_expire_days=1)
    db_session.add_all([channel, downloader, agent])
    await db_session.flush()

    stale_decision = PendingDecision(
        id=_uuid(), agent_id=agent.id,
        reason="stale", status="pending",
        expires_at=now - timedelta(days=1),
    )
    db_session.add(stale_decision)

    # Expired completed task with NO open delivery → deleted.
    old = await _seed_task(
        db_session, status="completed", agent=agent,
        completed_at=now - timedelta(days=10),
    )
    # Expired completed task WITH an open (non-done) delivery → survives.
    guarded = await _seed_task(
        db_session, status="completed", agent=agent,
        completed_at=now - timedelta(days=10),
    )
    # Old notification for the guarded task → its delivery keeps it alive;
    # an unguarded old notification → deleted.
    n = DownloadNotification(
        id=_uuid(), agent_id=agent.id, download_task_id=guarded.task.id,
        payload={}, created_at=now - timedelta(days=40),
    )
    webhook = AgentWebhook(
        id=_uuid(), agent_id=agent.id, url="http://consumer.invalid/hook",
        mock=True, enabled=True,
    )
    db_session.add_all([n, webhook])
    await db_session.commit()
    delivery = WebhookDelivery(
        id=_uuid(), notification_id=n.id, webhook_id=webhook.id,
        status="failed", next_attempt_at=now,
    )
    db_session.add(delivery)
    await db_session.commit()

    n2 = DownloadNotification(
        id=_uuid(), agent_id=agent.id, download_task_id=old.task.id,
        payload={}, created_at=now - timedelta(days=40),
    )
    db_session.add(n2)
    await db_session.commit()

    await scheduler._cleanup_expired()

    async with session_factory() as db:
        d = await db.get(PendingDecision, stale_decision.id)
        assert d.status == "expired"
        # The task with an open (non-done) delivery survives the age cut;
        # the unguarded one is deleted.
        assert await db.get(DownloadTask, old.task.id) is None
        assert await db.get(DownloadTask, guarded.task.id) is not None
        # Old notifications are deleted regardless (the open-delivery guard
        # protects the *task*, not the notification row itself).
        assert await db.get(DownloadNotification, n.id) is None
        assert await db.get(DownloadNotification, n2.id) is None
