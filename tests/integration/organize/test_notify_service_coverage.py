"""Notification service in-process integration coverage (complements the
scheduler-driven HTTP suite in tests/integration/http/test_notifications.py).

Directly exercises the notify pipeline pieces that the HTTP suite only
reaches through the per-minute scheduler tick:

  - backoff_delay / build_payload (pure builders, series + movie + bare work)
  - _build_snapshot: torrent RPC snapshot present / missing / RPC failure
  - create_notification_for_task: fresh create + idempotent re-create
  - ensure_deliveries: fan-out across multiple webhooks, disabled-webhook
    skip, agent scoping, no-op when no notifications/webhooks exist
  - deliver_due_deliveries: mock delivery, real-HTTP success, HTTP failure →
    backoff → failed after notify_max_attempts, disabled-webhook skip
  - regenerate_notifications: create missing, rebuild existing (payload
    replaced + non-pending deliveries reset), keep-old-snapshot when the
    torrent listing is unavailable, ``since`` filtering
  - reset_deliveries_for_retry: mode failed/all + notification scoping

Runs in-process against the per-test Turso engine (tests/unit/conftest.py
fixtures), same as tests/integration/organize. MVCC snapshot semantics mean
cross-phase reads must use fresh sessions (``session_factory``), so every
phase opens its own session rather than sharing ``db_session``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.agent_webhook import AgentWebhook
from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.webhook_delivery import WebhookDelivery
from app.services import notify_service
from app.services.notify_service import (
    _load_resource,
    backoff_delay,
    build_payload,
    create_notification_for_task,
    deliver_due_deliveries,
    ensure_deliveries,
    regenerate_notifications,
    reset_deliveries_for_retry,
)
from app.utils.time import utcnow


def _uuid() -> str:
    return str(uuid.uuid4())


async def _seed_chain(db, *, work, resource_kw=None, mock=True, enabled=True,
                      torrent_id: int | None = 42) -> SimpleNamespace:
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
    agent = Agent(
        id=_uuid(), name="agent", channel_id=channel.id,
        downloader_id=downloader.id,
    )
    db.add_all([channel, downloader, work, agent])
    await db.flush()
    webhook = AgentWebhook(
        id=_uuid(), agent_id=agent.id, url="http://consumer.invalid/hook",
        mock=mock, enabled=enabled,
    )
    db.add(webhook)
    resource_kw = dict(resource_kw or {})
    work_link = (
        {"series_id": work.id} if isinstance(work, TVSeries)
        else {"movie_id": work.id}
    )
    resource = FileResource(
        id=_uuid(), channel_id=channel.id, guid=_uuid(),
        title_raw=resource_kw.pop("title_raw", "raw"),
        torrent_url="magnet:?xt=urn:btih:abc", **work_link, **resource_kw,
    )
    task = DownloadTask(
        id=_uuid(), agent_id=agent.id, file_resource_id=resource.id,
        downloader_id=downloader.id, download_dir="/downloads",
        transmission_torrent_id=torrent_id, status="completed",
        completed_at=utcnow(),
    )
    db.add_all([resource, task])
    await db.commit()
    return SimpleNamespace(
        channel=channel, downloader=downloader, agent=agent, webhook=webhook,
        resource=resource, task=task, work=work,
    )


async def _get_deliveries(db, notification_id: str) -> list[WebhookDelivery]:
    return (
        await db.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.notification_id == notification_id
            )
        )
    ).scalars().all()


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------


async def test_backoff_delay_never_exceeds_cap():
    d0 = backoff_delay(0)
    assert d0.total_seconds() == notify_service.settings.notify_retry_base_seconds
    huge = backoff_delay(40)
    assert huge.total_seconds() == 1800
    assert backoff_delay(3).total_seconds() > backoff_delay(2).total_seconds()


async def test_build_payload_series_snapshot(db_session, session_factory):
    series = TVSeries(
        id=_uuid(), title_cn="攻壳机动队", title_en="Ghost in the Shell",
        start_date=None, content_type="tv", is_anime=True,
        genre=["Anime", "Fantasy"], seasons=[{"season_number": 1, "episode_count": 10}],
    )
    db_session.add(series)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=series, resource_kw=dict(
        season=1, episode=4, is_batch=False, subtitle_langs=["zh-CN"],
        resolution="1080p", container="mkv",
    ))
    async with session_factory() as db:
        resource = await _load_resource(db, chain.resource.id)
        torrent_info = {"name": "Show.S01", "files": [{"name": "ep04.mkv", "length": 300}]}
        payload = build_payload("nid-1", chain.agent, chain.task, resource, torrent_info)
    assert payload["notification_id"] == "nid-1"
    assert payload["work"]["type"] == "series"
    assert payload["work"]["genre"] == ["Animation", "Fantasy"]
    assert payload["work"]["year"] is None  # start_date None
    assert payload["resource"]["season"] == 1
    assert payload["resource"]["episode"] == 4
    assert payload["files"] == torrent_info["files"]
    assert payload["task"]["torrent_name"] == "Show.S01"
    # Without torrent_info → no files key.
    bare = build_payload("nid-2", chain.agent, chain.task, resource, None)
    assert "files" not in bare
    assert bare["task"]["torrent_name"] is None


async def test_build_payload_movie_and_empty_work(db_session, session_factory):
    movie = Movie(
        id=_uuid(), title_cn="哈姆奈特", title_en="Hamnet",
        release_date=None, content_type="movie", is_anime=False,
        genre=["Drama"],
    )
    db_session.add(movie)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=movie)
    async with session_factory() as db:
        resource = await _load_resource(db, chain.resource.id)
        payload = build_payload("nid-m", chain.agent, chain.task, resource, None)
    assert payload["work"]["type"] == "movie"
    assert payload["work"]["movie_id"] == movie.id
    assert payload["work"]["year"] is None
    # Bare work (no linked resource).
    empty = build_payload("nid-e", chain.agent, chain.task, None, None)
    assert empty["work"] == {"type": None}
    assert empty["resource"]["title_raw"] is None
    assert empty["agent"]["id"] == chain.agent.id


# ---------------------------------------------------------------------------
# create_notification_for_task + _build_snapshot
# ---------------------------------------------------------------------------


async def test_create_notification_for_task_and_idempotency(db_session, rpc_mocks):
    series = TVSeries(id=_uuid(), title_cn="攻壳机动队", title_en="Ghost in the Shell",
                      content_type="tv")
    db_session.add(series)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=series, resource_kw=dict(season=1, episode=4))
    rpc_mocks.get_files.return_value = {
        "name": "Show.S01",
        "files": [{"name": "ep04.mkv", "length": 300}],
    }

    notification, created = await create_notification_for_task(db_session, chain.task)
    assert created is True
    assert notification.agent_id == chain.agent.id
    assert notification.download_task_id == chain.task.id
    rpc_mocks.pause.assert_awaited_once_with(42)
    assert notification.payload["task"]["torrent_name"] == "Show.S01"

    # Idempotent: the same task yields the existing row, not a new one.
    again, was_created = await create_notification_for_task(db_session, chain.task)
    assert again.id == notification.id
    assert was_created is False

    # RPC failure → payload still built, no files key, snapshot=False.
    chain2 = await _seed_chain(db_session, work=series, resource_kw=dict(season=1, episode=5))
    rpc_mocks.get_files.side_effect = RuntimeError("rpc down")
    notification2, created2 = await create_notification_for_task(db_session, chain2.task)
    assert created2 is True
    assert "files" not in notification2.payload

    # No torrent id at all → skip RPC entirely.
    chain3 = await _seed_chain(db_session, work=series, torrent_id=None)
    notification3, created3 = await create_notification_for_task(db_session, chain3.task)
    assert created3 is True
    assert "files" not in notification3.payload
    assert rpc_mocks.pause.await_count == 2  # chain3 has no torrent id


# ---------------------------------------------------------------------------
# ensure_deliveries (fan-out)
# ---------------------------------------------------------------------------


async def test_ensure_deliveries_fanout_and_idempotency(db_session, session_factory):
    series = TVSeries(id=_uuid(), title_cn="剧集", title_en="Series", content_type="tv")
    db_session.add(series)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=series)
    async with session_factory() as db:
        notification, _ = await create_notification_for_task(db, chain.task)

    # A second enabled webhook on the same agent joins the fan-out.
    webhook2 = AgentWebhook(
        id=_uuid(), agent_id=chain.agent.id,
        url="http://consumer2.invalid/hook", mock=True, enabled=True,
    )
    db_session.add(webhook2)
    await db_session.commit()

    async with session_factory() as db:
        created = await ensure_deliveries(db)
        assert created == 2
        created = await ensure_deliveries(db)
        assert created == 0  # idempotent

        deliveries = await _get_deliveries(db, notification.id)
        assert {d.webhook_id for d in deliveries} == {chain.webhook.id, webhook2.id}
        assert all(d.status == "pending" for d in deliveries)


async def test_ensure_deliveries_agent_scope_and_disabled_webhook(db_session, session_factory):
    series = TVSeries(id=_uuid(), title_cn="剧集", title_en="Series", content_type="tv")
    db_session.add(series)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=series)
    async with session_factory() as db:
        await create_notification_for_task(db, chain.task)

    # Disabled webhook is not fanned out.
    chain.webhook.enabled = False
    await db_session.commit()
    async with session_factory() as db:
        assert await ensure_deliveries(db) == 0
        # Scoping to a foreign agent finds no notifications.
        assert await ensure_deliveries(db, agent_id="no-such-agent") == 0


async def test_ensure_deliveries_no_notifications_or_webhooks(session_factory):
    async with session_factory() as db:
        assert await ensure_deliveries(db) == 0


# ---------------------------------------------------------------------------
# deliver_due_deliveries
# ---------------------------------------------------------------------------


async def test_deliver_mock_webhook_marks_done(db_session, session_factory, rpc_mocks):
    series = TVSeries(id=_uuid(), title_cn="剧集", title_en="Series", content_type="tv")
    db_session.add(series)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=series)
    async with session_factory() as db:
        notification, _ = await create_notification_for_task(db, chain.task)
        await ensure_deliveries(db)
        stats = await deliver_due_deliveries(db)
        assert stats == {"delivered": 1, "failed": 0, "skipped": 0}
        deliveries = await _get_deliveries(db, notification.id)
        assert deliveries[0].status == "done"
        assert deliveries[0].delivered_at is not None


async def test_deliver_disabled_webhook_skipped(db_session, session_factory, rpc_mocks):
    series = TVSeries(id=_uuid(), title_cn="剧集", title_en="Series", content_type="tv")
    db_session.add(series)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=series)
    async with session_factory() as db:
        await create_notification_for_task(db, chain.task)
        await ensure_deliveries(db)
    chain.webhook.enabled = False
    await db_session.commit()
    async with session_factory() as db:
        stats = await deliver_due_deliveries(db)
        assert stats["skipped"] == 1
        rows = (
            await db.execute(
                select(WebhookDelivery).where(WebhookDelivery.status == "pending")
            )
        ).scalars().all()
        assert len(rows) == 1


class _FakePost:
    def __init__(self, response, error=None):
        self._response = response
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error
        if self._response.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=httpx.Request("POST", "http://x"), response=self._response
            )

    @property
    def status_code(self):
        return self._response.status_code


class _FakeAsyncClient:
    def __init__(self, *, ok=True, error=None):
        self._ok = ok
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        return _FakePost(httpx.Response(200 if self._ok else 500), error=self._error)


@pytest.fixture
def fake_http(monkeypatch):
    state = {"calls": 0}

    def make_client(**kw):
        def factory(*a, **k):
            state["calls"] += 1
            return _FakeAsyncClient(**kw)

        return factory

    def install(**kw):
        monkeypatch.setattr(
            "app.services.notify_service.httpx.AsyncClient", make_client(**kw)
        )

    return SimpleNamespace(install=install, calls=state)


@pytest.fixture
def zero_backoff(monkeypatch):
    """Collapse exponential backoff so failed deliveries stay due instantly."""
    monkeypatch.setattr(notify_service.settings, "notify_retry_base_seconds", 0)


async def test_deliver_http_success(db_session, session_factory, rpc_mocks, fake_http):
    fake_http.install(ok=True)
    series = TVSeries(id=_uuid(), title_cn="剧集", title_en="Series", content_type="tv")
    db_session.add(series)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=series, mock=False)
    async with session_factory() as db:
        notification, _ = await create_notification_for_task(db, chain.task)
        await ensure_deliveries(db)
        stats = await deliver_due_deliveries(db)
        assert stats == {"delivered": 1, "failed": 0, "skipped": 0}
        assert fake_http.calls["calls"] == 1
        deliveries = await _get_deliveries(db, notification.id)
        assert deliveries[0].status == "done"


async def test_deliver_http_failure_backoff_then_failed(
    db_session, session_factory, rpc_mocks, fake_http, zero_backoff
):
    fake_http.install(ok=False)
    series = TVSeries(id=_uuid(), title_cn="剧集", title_en="Series", content_type="tv")
    db_session.add(series)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=series, mock=False)
    async with session_factory() as db:
        notification, _ = await create_notification_for_task(db, chain.task)
        await ensure_deliveries(db)

        # Attempt 1 → backoff (attempt_count < notify_max_attempts).
        stats = await deliver_due_deliveries(db)
        assert stats["skipped"] == 1
        d = (await _get_deliveries(db, notification.id))[0]
        assert d.status == "pending"
        assert d.attempt_count == 1
        assert "退避" in d.error_message

        # Remaining attempts (2..max) → failed after the cap is reached.
        for _ in range(notify_service.settings.notify_max_attempts - 1):
            await deliver_due_deliveries(db)
        d = (await _get_deliveries(db, notification.id))[0]
        assert d.status == "failed"
        assert "已达最大重试次数" in d.error_message


# ---------------------------------------------------------------------------
# reset_deliveries_for_retry
# ---------------------------------------------------------------------------


async def test_reset_deliveries_modes_and_scoping(db_session, session_factory, rpc_mocks):
    series = TVSeries(id=_uuid(), title_cn="剧集", title_en="Series", content_type="tv")
    db_session.add(series)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=series)
    webhook2 = AgentWebhook(
        id=_uuid(), agent_id=chain.agent.id,
        url="http://consumer2.invalid/hook", mock=True, enabled=True,
    )
    db_session.add(webhook2)
    await db_session.commit()
    async with session_factory() as db:
        notification, _ = await create_notification_for_task(db, chain.task)
        await ensure_deliveries(db)
        rows = await _get_deliveries(db, notification.id)
        assert len(rows) == 2
        rows[0].status = "done"
        rows[1].status = "failed"
        await db.commit()

        # mode=failed resets only the failed one.
        n = await reset_deliveries_for_retry(db, "failed", notification_id=notification.id)
        assert n == 1
        refreshed = await _get_deliveries(db, notification.id)
        assert sorted(r.status for r in refreshed) == ["done", "pending"]
        reset_row = next(r for r in refreshed if r.status == "pending")
        assert reset_row.attempt_count == 0
        assert reset_row.next_attempt_at is not None

        # mode=all resets the remaining done delivery.
        n = await reset_deliveries_for_retry(db, "all", notification_id=notification.id)
        assert n == 1
        refreshed = await _get_deliveries(db, notification.id)
        assert all(r.status == "pending" for r in refreshed)

        # Foreign scoping matches nothing.
        assert await reset_deliveries_for_retry(db, "all", agent_id="no-such-agent") == 0
        assert await reset_deliveries_for_retry(db, "all", since=utcnow()) == 0


# ---------------------------------------------------------------------------
# regenerate_notifications
# ---------------------------------------------------------------------------


async def test_regenerate_creates_missing_and_rebuilds(db_session, session_factory, rpc_mocks):
    series = TVSeries(id=_uuid(), title_cn="攻壳机动队", title_en="Ghost in the Shell",
                      content_type="tv")
    db_session.add(series)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=series, resource_kw=dict(season=1, episode=4))
    rpc_mocks.get_files.return_value = {
        "name": "Show.S01",
        "files": [{"name": "ep04.mkv", "length": 300}],
    }

    # No notification yet → regenerate creates it.
    async with session_factory() as db:
        stats = await regenerate_notifications(db, chain.agent.id, None)
        assert stats == {"created": 1, "regenerated": 0}
        notification = (await db.execute(select(notify_service.DownloadNotification))).scalar_one()

    # A second run rebuilds the existing snapshot and resets non-pending
    # deliveries. First mark the delivery done.
    async with session_factory() as db:
        await ensure_deliveries(db)
        await deliver_due_deliveries(db)
    async with session_factory() as db:
        stats = await regenerate_notifications(db, chain.agent.id, None)
        assert stats == {"created": 0, "regenerated": 1}
        deliveries = await _get_deliveries(db, notification.id)
        assert len(deliveries) == 1
        assert deliveries[0].status == "pending"
        assert deliveries[0].attempt_count == 0

    # since in the future → nothing in scope.
    async with session_factory() as db:
        assert await regenerate_notifications(db, chain.agent.id, utcnow()) == {
            "created": 0,
            "regenerated": 0,
        }


async def test_regenerate_keeps_old_snapshot_when_rpc_unavailable(
    db_session, session_factory, rpc_mocks
):
    series = TVSeries(id=_uuid(), title_cn="攻壳机动队", title_en="Ghost in the Shell",
                      content_type="tv")
    db_session.add(series)
    await db_session.flush()
    chain = await _seed_chain(db_session, work=series, resource_kw=dict(season=1, episode=4))
    rpc_mocks.get_files.return_value = {
        "name": "Show.S01",
        "files": [{"name": "ep04.mkv", "length": 300}],
    }
    async with session_factory() as db:
        notification, _ = await create_notification_for_task(db, chain.task)
        original = dict(notification.payload)

    # RPC now fails: regenerate must keep the old snapshot, not degrade it.
    rpc_mocks.get_files.side_effect = RuntimeError("rpc down")
    async with session_factory() as db:
        stats = await regenerate_notifications(db, chain.agent.id, None)
        assert stats == {"created": 0, "regenerated": 0}
        fresh = (await db.execute(select(notify_service.DownloadNotification))).scalar_one()
        assert fresh.payload == original
