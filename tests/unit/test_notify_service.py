"""Unit tests for app.services.notify_service.

Covers the pure payload builder, notification creation (idempotency, torrent
pause + file-listing snapshot), delivery fan-out (idempotency, backlog for
newly added webhooks), webhook delivery (mock / disabled / success /
concurrent isolation / backoff exhaustion), retry resets, retention cleanup,
and the scheduler tick. Downloader RPC is exercised through the mock
downloader; HTTP delivery is stubbed at httpx.AsyncClient.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.agent import Agent
from app.models.agent_webhook import AgentWebhook
from app.models.channel import Channel
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.models.webhook_delivery import WebhookDelivery
from app.services import notify_service
from app.services.notify_service import (
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


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


# ---------------------------------------------------------------------------
# Pure payload builder
# ---------------------------------------------------------------------------


def _series_ns(**overrides):
    defaults = dict(
        id="series-1",
        title_en="Frieren",
        title_cn="葬送的芙莉莲",
        original_title="葬送のフリーレン",
        start_date=date(2023, 9, 29),
        content_type="anime",
        is_anime=True,
        collection=SimpleNamespace(display_name="Frieren"),
        genre=["Anime", "Fantasy"],  # "Anime" alias normalizes to "Animation"
        seasons=[{"season_number": 1, "episode_count": 28}],
        episodes=[
            SimpleNamespace(season=1, episode=1, title="冒险的结束"),
            SimpleNamespace(season=1, episode=2, title="倒也不是魔法…"),
        ],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _resource_ns(series=None, movie=None, **overrides):
    defaults = dict(
        title_raw="[Group] Frieren - 05 [1080p]",
        season=1,
        episode=5,
        is_batch=False,
        batch_scope=None,
        collection=None,
        episode_start=None,
        episode_end=None,
        subtitle_langs=["zh-CN"],
        resolution="1080p",
        container="MKV",
        title_year=2023,
        series=series,
        movie=movie,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_build_payload_series():
    payload = build_payload(
        "notif-1",
        SimpleNamespace(id="agent-1", name="My Agent"),
        SimpleNamespace(id="task-1", download_dir="/downloads/anime"),
        _resource_ns(series=_series_ns()),
        {"name": "Frieren.S01", "files": [{"name": "ep05.mkv", "size": 100}]},
    )
    assert payload["notification_id"] == "notif-1"
    assert payload["agent"] == {"id": "agent-1", "name": "My Agent"}
    assert payload["task"]["download_dir"] == "/downloads/anime"
    assert payload["task"]["torrent_name"] == "Frieren.S01"
    assert payload["resource"]["episode"] == 5
    assert payload["resource"]["subtitle_langs"] == ["zh-CN"]
    work = payload["work"]
    assert work["type"] == "series"
    assert work["title_en"] == "Frieren"
    assert work["year"] == 2023
    assert work["content_type"] == "anime"
    assert work["is_anime"] is True
    assert work["collection"] == "Frieren"
    assert work["genre"] == ["Animation", "Fantasy"]
    assert work["seasons"] == [{"season_number": 1, "episode_count": 28}]
    assert work["episodes"][0] == {"season": 1, "episode": 1, "title": "冒险的结束"}
    assert payload["files"] == [{"name": "ep05.mkv", "size": 100}]


def test_build_payload_movie():
    movie = SimpleNamespace(
        id="movie-1",
        title_en="Your Name",
        title_cn="你的名字",
        original_title="君の名は。",
        release_date=date(2016, 8, 26),
        content_type="movie",
        is_anime=False,
        collection=None,
        genre=["Romance", "Animation"],
    )
    payload = build_payload(
        "notif-2", None, SimpleNamespace(id="t", download_dir="/downloads"),
        _resource_ns(movie=movie, season=None, episode=None),
        {"name": "Your.Name.2016", "files": []},
    )
    assert payload["agent"] is None
    work = payload["work"]
    assert work["type"] == "movie"
    assert work["year"] == 2016
    assert work["is_anime"] is False
    assert work["collection"] is None
    assert work["genre"] == ["Romance", "Animation"]
    assert work["episodes"] is None


def test_build_payload_without_torrent_info():
    payload = build_payload(
        "notif-3", None, SimpleNamespace(id="t", download_dir="/d"),
        _resource_ns(), None,
    )
    assert "files" not in payload
    assert payload["task"]["torrent_name"] is None


def test_build_payload_freezes_complete_file_associations():
    assignment = SimpleNamespace(
        file_path="Frieren.S01E05.mkv", file_size=100,
        series_id="series-1", movie_id=None, season=1,
        episode_start=5, episode_end=5, source="manual",
    )
    payload = build_payload(
        "notif-assignment", None,
        SimpleNamespace(id="t", download_dir="/d"),
        _resource_ns(id="resource-1", series=_series_ns(), file_assignments=[assignment]),
        {"name": "Frieren", "files": [{"name": "Frieren.S01E05.mkv", "size": 100}]},
    )
    assert payload["resource"]["id"] == "resource-1"
    assert payload["file_associations"]["status"] == "complete"
    assert payload["file_associations"]["items"][0]["source"] == "manual"


def test_build_payload_freezes_all_linked_work_metadata():
    first = _series_ns()
    second = _series_ns()
    second.id = "series-2"
    second.title_cn = "第二部作品"
    resource = _resource_ns(
        id="resource-multi", series=None, movie=None,
        file_assignments=[],
        work_links=[
            SimpleNamespace(series=first, movie=None),
            SimpleNamespace(series=second, movie=None),
        ],
    )
    payload = build_payload(
        "notif-multi", None, SimpleNamespace(id="t", download_dir="/d"),
        resource, {"name": "pack", "files": []},
    )
    assert set(payload["works"]) == {"series:series-1", "series:series-2"}
    assert payload["works"]["series:series-2"]["title_cn"] == "第二部作品"


def test_backoff_delay_grows_and_caps(monkeypatch):
    monkeypatch.setattr(settings, "notify_retry_base_seconds", 30)
    assert backoff_delay(1).total_seconds() == 60
    assert backoff_delay(2).total_seconds() == 120
    assert backoff_delay(20).total_seconds() == notify_service.MAX_BACKOFF_SECONDS


def test_webhook_timeout_is_three_minutes():
    assert notify_service.WEBHOOK_TIMEOUT_SECONDS == 180.0


# ---------------------------------------------------------------------------
# DB-backed tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def seed(db_session):
    """Channel + mock downloader + agent + series + resource + completed task.

    Also registers a torrent in the mock downloader store so the file-listing
    RPC path returns real data. No webhooks by default — tests add their own.
    """
    from app.clients.mock_downloader import MockDownloaderWrapper, reset_state

    reset_state()
    channel = Channel(
        id=_uuid(), name="Ch", type="rss_feed", url="https://x/rss",
        fetch_interval=1800, status="active", field_mapping=TEST_FIELD_MAPPING,
        metadata_agent_enabled=False,
    )
    downloader = DownloaderInstance(
        id=_uuid(), name="DL", type="mock", url="mock://local",
        download_dir="/downloads/rssripple", status="connected",
    )
    agent = Agent(
        id=_uuid(), name="Agent", channel_id=channel.id,
        downloader_id=downloader.id,
    )
    series = TVSeries(
        id=_uuid(), title_en="Test Series", title_cn="测试剧集",
        content_type="tv",
    )
    resource = FileResource(
        id=_uuid(), channel_id=channel.id, guid="g1",
        title_raw="[G] Test Series - 05 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc&dn=Test.Series.S01",
        series_id=series.id, season=1, episode=5, is_batch=False,
    )
    db_session.add_all([channel, downloader, agent, series, resource])
    await db_session.flush()
    db_session.add(Episode(
        id=_uuid(), series_id=series.id, season=1, episode=5, title="第五集",
    ))

    wrapper = MockDownloaderWrapper(downloader=downloader)
    added = await wrapper.add_torrent(
        resource.torrent_url, download_dir="/downloads/rssripple"
    )
    task = DownloadTask(
        id=_uuid(), agent_id=agent.id, file_resource_id=resource.id,
        downloader_id=downloader.id, download_dir="/downloads/rssripple",
        transmission_torrent_id=added["torrent_id"], status="completed",
        completed_at=utcnow(),
    )
    db_session.add(task)
    await db_session.commit()
    return SimpleNamespace(
        channel=channel, downloader=downloader, agent=agent,
        series=series, resource=resource, task=task,
    )


def _webhook(agent_id: str, **overrides) -> AgentWebhook:
    defaults = dict(
        id=_uuid(), agent_id=agent_id,
        url="http://organizer:8910/webhook", mock=False, enabled=True,
    )
    defaults.update(overrides)
    return AgentWebhook(**defaults)


async def _deliveries(db_session, notification_id: str | None = None):
    stmt = select(WebhookDelivery)
    if notification_id is not None:
        stmt = stmt.where(WebhookDelivery.notification_id == notification_id)
    return (await db_session.execute(stmt)).scalars().all()


async def test_create_notification_snapshots_and_pauses(db_session, seed):
    n, created = await create_notification_for_task(db_session, seed.task)
    await db_session.commit()

    assert created is True
    assert n.agent_id == seed.agent.id
    assert n.payload["notification_id"] == n.id
    assert n.payload["work"]["title_en"] == "Test Series"
    assert n.payload["work"]["episodes"] == [
        {"season": 1, "episode": 5, "title": "第五集"}
    ]
    assert n.payload["files"] == [
        {"name": "Test.Series.S01", "size": 1_000_000_000}
    ]
    # Torrent was stopped (mock state reflects the pause).
    from app.clients.mock_downloader import _store

    state = _store(seed.downloader.id).torrents[seed.task.transmission_torrent_id]
    assert state.paused is True


async def test_create_notification_idempotent(db_session, seed):
    first, created1 = await create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    second, created2 = await create_notification_for_task(db_session, seed.task)
    assert created1 is True and created2 is False
    assert second.id == first.id


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


async def test_ensure_deliveries_fans_out_to_enabled_webhooks_only(
    db_session, seed
):
    n, _ = await create_notification_for_task(db_session, seed.task)
    db_session.add_all([
        _webhook(seed.agent.id, url="http://a/hook"),
        _webhook(seed.agent.id, url="http://b/hook"),
        _webhook(seed.agent.id, url="http://disabled/hook", enabled=False),
    ])
    await db_session.commit()

    created = await ensure_deliveries(db_session)
    assert created == 2
    rows = await _deliveries(db_session, n.id)
    assert len(rows) == 2
    assert all(d.status == "pending" for d in rows)
    assert all(d.next_attempt_at is not None for d in rows)

    # Idempotent: a second fan-out creates nothing.
    assert await ensure_deliveries(db_session) == 0
    assert len(await _deliveries(db_session, n.id)) == 2


async def test_ensure_deliveries_new_webhook_receives_backlog(db_session, seed):
    n, _ = await create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    assert await ensure_deliveries(db_session) == 0  # no webhooks yet

    db_session.add(_webhook(seed.agent.id))
    await db_session.commit()
    assert await ensure_deliveries(db_session) == 1
    rows = await _deliveries(db_session, n.id)
    assert len(rows) == 1 and rows[0].status == "pending"


async def test_ensure_deliveries_scoped_by_agent(db_session, seed):
    n, _ = await create_notification_for_task(db_session, seed.task)
    other_agent = Agent(
        id=_uuid(), name="Other", channel_id=seed.channel.id,
        downloader_id=seed.downloader.id,
    )
    db_session.add(other_agent)
    await db_session.flush()
    db_session.add(_webhook(other_agent.id))
    db_session.add(_webhook(seed.agent.id))
    await db_session.commit()

    # Scoped to the other agent: seed's notification gets no delivery.
    assert await ensure_deliveries(db_session, agent_id=other_agent.id) == 0
    assert await ensure_deliveries(db_session, agent_id=seed.agent.id) == 1
    assert len(await _deliveries(db_session, n.id)) == 1


async def test_ensure_deliveries_skips_agentless_notifications(db_session, seed):
    n, _ = await create_notification_for_task(db_session, seed.task)
    n.agent_id = None  # Agent 被删除（SET NULL）后的历史通知
    db_session.add(_webhook(seed.agent.id))
    await db_session.commit()
    assert await ensure_deliveries(db_session) == 0
    assert await _deliveries(db_session, n.id) == []


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def test_deliver_mock_webhook_done_without_http(db_session, seed):
    db_session.add(_webhook(seed.agent.id, mock=True, url="http://unused/x"))
    n, _ = await create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    await ensure_deliveries(db_session)

    stats = await deliver_due_deliveries(db_session)
    assert stats == {"delivered": 1, "failed": 0, "skipped": 0}
    d = (await _deliveries(db_session, n.id))[0]
    assert d.status == "done"
    assert d.delivered_at is not None


async def test_deliver_disabled_webhook_skipped(db_session, seed):
    webhook = _webhook(seed.agent.id)
    db_session.add(webhook)
    n, _ = await create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    await ensure_deliveries(db_session)

    webhook.enabled = False
    await db_session.commit()
    stats = await deliver_due_deliveries(db_session)
    assert stats["skipped"] == 1 and stats["delivered"] == 0
    d = (await _deliveries(db_session, n.id))[0]
    assert d.status == "pending"  # waits for re-enable


class _FakeResp:
    def raise_for_status(self):
        return None


def _stub_httpx(monkeypatch, calls: list, fail_urls: set[str] | None = None):
    import httpx

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append((url, json))
            if fail_urls and url in fail_urls:
                raise httpx.ConnectError("connection refused")
            return _FakeResp()

    monkeypatch.setattr(notify_service.httpx, "AsyncClient", _FakeClient)


async def test_deliver_http_success(db_session, seed, monkeypatch):
    db_session.add(_webhook(seed.agent.id))
    n, _ = await create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    await ensure_deliveries(db_session)

    calls: list = []
    _stub_httpx(monkeypatch, calls)
    stats = await deliver_due_deliveries(db_session)

    assert stats["delivered"] == 1
    assert len(calls) == 1
    url, body = calls[0]
    assert url == "http://organizer:8910/webhook"
    assert body["event"] == "download.completed"
    assert body["notification"]["notification_id"] == n.id
    d = (await _deliveries(db_session, n.id))[0]
    assert d.status == "done" and d.delivered_at is not None


async def test_deliver_concurrent_isolation(db_session, seed, monkeypatch):
    """One webhook succeeds, another fails → per-delivery statuses, no
    cross-contamination from the shared session."""
    ok_url = "http://ok/hook"
    bad_url = "http://bad/hook"
    db_session.add_all([
        _webhook(seed.agent.id, url=ok_url),
        _webhook(seed.agent.id, url=bad_url),
    ])
    n, _ = await create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    await ensure_deliveries(db_session)

    calls: list = []
    _stub_httpx(monkeypatch, calls, fail_urls={bad_url})
    stats = await deliver_due_deliveries(db_session)

    assert stats["delivered"] == 1
    assert stats["skipped"] == 1  # failed attempt, backoff scheduled
    assert stats["failed"] == 0
    rows = {d.webhook_id: d for d in await _deliveries(db_session, n.id)}
    webhooks = (await db_session.execute(select(AgentWebhook))).scalars().all()
    by_url = {w.url: w.id for w in webhooks}
    ok_d = rows[by_url[ok_url]]
    bad_d = rows[by_url[bad_url]]
    assert ok_d.status == "done" and ok_d.delivered_at is not None
    assert bad_d.status == "pending"
    assert bad_d.attempt_count == 1
    assert bad_d.next_attempt_at is not None
    assert "退避重试" in (bad_d.error_message or "")


async def test_deliver_failure_backoff_then_failed(db_session, seed, monkeypatch):
    monkeypatch.setattr(settings, "notify_max_attempts", 2)
    db_session.add(_webhook(seed.agent.id))
    n, _ = await create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    await ensure_deliveries(db_session)
    d = (await _deliveries(db_session, n.id))[0]

    calls: list = []
    _stub_httpx(monkeypatch, calls, fail_urls={"http://organizer:8910/webhook"})

    stats = await deliver_due_deliveries(db_session)
    assert stats["failed"] == 0
    assert d.attempt_count == 1
    assert d.status == "pending"
    assert d.next_attempt_at is not None

    # Force the row due again and let the second failure exhaust attempts.
    d.next_attempt_at = utcnow()
    await db_session.commit()
    stats = await deliver_due_deliveries(db_session)
    assert stats["failed"] == 1
    assert d.status == "failed"
    assert "最大重试次数" in (d.error_message or "")


# ---------------------------------------------------------------------------
# Retry resets
# ---------------------------------------------------------------------------


@pytest.fixture
async def retry_seed(db_session, seed):
    """Two notifications with deliveries in mixed states."""
    n1, _ = await create_notification_for_task(db_session, seed.task)

    task2 = DownloadTask(
        id=_uuid(), agent_id=seed.agent.id,
        file_resource_id=seed.resource.id, downloader_id=seed.downloader.id,
        download_dir="/downloads/rssripple",
        transmission_torrent_id=None, status="completed",
        completed_at=utcnow(),
    )
    db_session.add(task2)
    await db_session.flush()
    n2 = DownloadNotification(
        id=_uuid(), agent_id=seed.agent.id, download_task_id=task2.id,
        payload={"notification_id": "n2"},
        created_at=utcnow() - timedelta(days=10),
    )
    db_session.add(n2)
    webhook = _webhook(seed.agent.id)
    db_session.add(webhook)
    await db_session.flush()
    d_failed = WebhookDelivery(
        id=_uuid(), notification_id=n1.id, webhook_id=webhook.id,
        status="failed", attempt_count=5, error_message="boom",
    )
    d_done = WebhookDelivery(
        id=_uuid(), notification_id=n2.id, webhook_id=webhook.id,
        status="done", delivered_at=utcnow() - timedelta(days=9),
    )
    db_session.add_all([d_failed, d_done])
    await db_session.commit()
    return SimpleNamespace(n1=n1, n2=n2, webhook=webhook,
                           d_failed=d_failed, d_done=d_done)


async def test_reset_failed_mode_only_resets_failed(db_session, retry_seed):
    reset = await reset_deliveries_for_retry(db_session, "failed")
    assert reset == 1
    assert retry_seed.d_failed.status == "pending"
    assert retry_seed.d_failed.attempt_count == 0
    assert retry_seed.d_failed.error_message is None
    assert retry_seed.d_failed.next_attempt_at is not None
    assert retry_seed.d_done.status == "done"


async def test_reset_all_mode_resets_done_and_failed(db_session, retry_seed):
    reset = await reset_deliveries_for_retry(db_session, "all")
    assert reset == 2
    assert retry_seed.d_failed.status == "pending"
    assert retry_seed.d_done.status == "pending"
    assert retry_seed.d_done.attempt_count == 0


async def test_reset_since_filter(db_session, retry_seed):
    # n2 was created 10 days ago; n1 just now. since=yesterday → only n1.
    reset = await reset_deliveries_for_retry(
        db_session, "all", since=utcnow() - timedelta(days=1)
    )
    assert reset == 1
    assert retry_seed.d_failed.status == "pending"
    assert retry_seed.d_done.status == "done"


async def test_reset_agent_and_notification_filters(db_session, retry_seed):
    # Unknown agent → nothing.
    assert await reset_deliveries_for_retry(
        db_session, "all", agent_id=_uuid()
    ) == 0
    # Scoped to n2 → only its delivery.
    reset = await reset_deliveries_for_retry(
        db_session, "all", notification_id=retry_seed.n2.id
    )
    assert reset == 1
    assert retry_seed.d_done.status == "pending"
    assert retry_seed.d_failed.status == "failed"


# ---------------------------------------------------------------------------
# Regenerate（"重新生成"：对范围内任务重跑完整生成链路）
# ---------------------------------------------------------------------------


async def test_regenerate_creates_missing_and_rebuilds_existing(
    db_session, seed,
):
    # Task 1 already has a notification → payload is rebuilt, not skipped.
    n, _ = await create_notification_for_task(db_session, seed.task)

    # Task 2: older completed task without a notification → created.
    older = DownloadTask(
        id=_uuid(), agent_id=seed.agent.id,
        file_resource_id=seed.resource.id, downloader_id=seed.downloader.id,
        download_dir="/downloads/rssripple",
        transmission_torrent_id=None, status="completed",
        completed_at=utcnow().replace(year=2020),
    )
    db_session.add(older)
    await db_session.commit()

    stats = await regenerate_notifications(db_session, seed.agent.id, None)
    assert stats == {"created": 1, "regenerated": 1}

    rows = (await db_session.execute(select(DownloadNotification))).scalars().all()
    assert len(rows) == 2
    # The existing notification keeps its id (payload["notification_id"] stable).
    await db_session.refresh(n)
    assert n.payload["notification_id"] == n.id

    # since after both tasks → nothing in scope.
    stats = await regenerate_notifications(db_session, seed.agent.id, utcnow())
    assert stats == {"created": 0, "regenerated": 0}


async def test_regenerate_batches_commits(db_session, seed):
    """超过批次大小的 regenerate 正常分批提交。"""
    tasks = [
        DownloadTask(
            id=_uuid(), agent_id=seed.agent.id,
            file_resource_id=seed.resource.id,
            downloader_id=seed.downloader.id,
            download_dir="/downloads/rssripple",
            transmission_torrent_id=None, status="completed",
            completed_at=utcnow(),
        )
        for _ in range(25)
    ]
    db_session.add_all(tasks)
    await db_session.commit()
    stats = await regenerate_notifications(db_session, seed.agent.id, None)
    # 25 个新任务 + seed 自带的 1 个 completed 任务
    assert stats == {"created": 26, "regenerated": 0}


def _strip_files(n: DownloadNotification) -> None:
    """模拟缺陷期创建的快照：整段缺失 downloader-RPC 派生字段。"""
    payload = dict(n.payload)
    payload.pop("files", None)
    payload["task"] = {**payload["task"], "torrent_name": None}
    n.payload = payload


async def test_regenerate_restores_files_and_resets_deliveries(
    db_session, seed,
):
    """回归：payload 缺 files 的存量通知经重新生成恢复 files，且其非
    pending delivery 被复位为立即到期的 pending（新快照会重新投递）。"""
    n, _ = await create_notification_for_task(db_session, seed.task)
    _strip_files(n)
    webhook = _webhook(seed.agent.id)
    db_session.add(webhook)
    await db_session.flush()
    done = WebhookDelivery(
        id=_uuid(), notification_id=n.id, webhook_id=webhook.id,
        status="done", attempt_count=3, delivered_at=utcnow(),
    )
    db_session.add(done)
    await db_session.commit()

    stats = await regenerate_notifications(db_session, seed.agent.id, None)

    assert stats == {"created": 0, "regenerated": 1}
    await db_session.refresh(n)
    assert n.payload["files"] == [
        {"name": "Test.Series.S01", "size": 1_000_000_000}
    ]
    assert n.payload["task"]["torrent_name"] == "Test.Series.S01"
    assert n.payload["notification_id"] == n.id
    await db_session.refresh(done)
    assert done.status == "pending"
    assert done.attempt_count == 0


async def test_regenerate_reruns_intact_snapshots(db_session, seed):
    """重新生成无条件重跑生成链路：完好快照同样被重建并重投。"""
    n, _ = await create_notification_for_task(db_session, seed.task)
    webhook = _webhook(seed.agent.id)
    db_session.add(webhook)
    await db_session.flush()
    done = WebhookDelivery(
        id=_uuid(), notification_id=n.id, webhook_id=webhook.id,
        status="done", attempt_count=1, delivered_at=utcnow(),
    )
    db_session.add(done)
    await db_session.commit()

    stats = await regenerate_notifications(db_session, seed.agent.id, None)

    assert stats == {"created": 0, "regenerated": 1}
    await db_session.refresh(done)
    assert done.status == "pending"


async def test_regenerate_torrent_unavailable_keeps_payload(db_session, seed):
    """种子已从下载器删除：生成链路拿不到文件清单，保留旧快照不降级。"""
    from app.clients.mock_downloader import _store

    n, _ = await create_notification_for_task(db_session, seed.task)
    original_files = n.payload["files"]
    webhook = _webhook(seed.agent.id)
    db_session.add(webhook)
    await db_session.flush()
    done = WebhookDelivery(
        id=_uuid(), notification_id=n.id, webhook_id=webhook.id,
        status="done", delivered_at=utcnow(),
    )
    db_session.add(done)
    await db_session.commit()
    _store(seed.downloader.id).torrents.pop(seed.task.transmission_torrent_id)

    stats = await regenerate_notifications(db_session, seed.agent.id, None)

    assert stats == {"created": 0, "regenerated": 0}
    await db_session.refresh(n)
    assert n.payload["files"] == original_files
    await db_session.refresh(done)
    assert done.status == "done"  # 未重建，投递不被打扰


# ---------------------------------------------------------------------------
# 边界与调度 tick
# ---------------------------------------------------------------------------


async def test_create_notification_rpc_degraded(db_session, seed, monkeypatch):
    """Transmission RPC 失败时降级：不带 files 入队，不阻塞通知创建。"""
    from unittest.mock import AsyncMock

    # 让 mock downloader 的 get_torrent_files 也失败，模拟 RPC 不可达。
    monkeypatch.setattr(
        "app.clients.mock_downloader.MockDownloaderWrapper.get_torrent_files",
        AsyncMock(side_effect=Exception("rpc down")),
    )
    monkeypatch.setattr(
        "app.clients.mock_downloader.MockDownloaderWrapper.pause_torrent",
        AsyncMock(side_effect=Exception("rpc down")),
    )
    n, created = await create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    assert created is True
    assert "files" not in n.payload
    assert n.payload["task"]["torrent_name"] is None


async def test_scheduler_tick_enqueues_fans_out_and_delivers(db_session, seed):
    """_process_download_notifications：为 completed 任务补建通知 → 扇出
    delivery → 投递（mock webhook 直接 done）。"""
    from app.services.scheduler import _process_download_notifications

    db_session.add(_webhook(seed.agent.id, mock=True, url="http://unused/x"))
    await db_session.commit()
    # seed 的任务尚无通知（该 fixture 不创建通知行）
    assert (
        await db_session.execute(select(DownloadNotification))
    ).scalars().all() == []

    await _process_download_notifications()

    rows = (
        await db_session.execute(select(DownloadNotification))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload["work"]["title_en"] == "Test Series"
    deliveries = await _deliveries(db_session, rows[0].id)
    assert len(deliveries) == 1
    assert deliveries[0].status == "done"  # mock 投递成功
    assert deliveries[0].delivered_at is not None


async def test_scheduler_tick_skips_agents_without_webhook(db_session, seed):
    """agent 没有启用的 webhook 时，tick 不为其 completed 任务生成通知。"""
    from app.services.scheduler import _process_download_notifications

    # seed 不注册任何 webhook
    await _process_download_notifications()
    assert (
        await db_session.execute(select(DownloadNotification))
    ).scalars().all() == []

    # 仅 disabled webhook 同样不生成
    db_session.add(_webhook(seed.agent.id, mock=True, url="http://unused/x", enabled=False))
    await db_session.commit()
    await _process_download_notifications()
    assert (
        await db_session.execute(select(DownloadNotification))
    ).scalars().all() == []


async def test_scheduler_tick_creates_notification_for_organize_without_webhook(
    db_session, seed,
):
    """agent 无启用 webhook，但存在启用的 organize 规则时，tick 仍生成通知
    （organize 是通知快照的内置消费者），只是不产生任何 delivery。"""
    from app.models.library import Library
    from app.models.organize_rule import OrganizeRule
    from app.services.scheduler import _process_download_notifications

    library = Library(id=_uuid(), name="TV", kind="tv")  # 未绑定卷即可
    rule = OrganizeRule(
        id=_uuid(), name="tv", priority=100, enabled=True, filter=None,
        library_id=library.id, path_template="{title}/S{season:02d}",
        file_op="move", auto_execute=False,
    )
    db_session.add_all([library, rule])
    await db_session.commit()

    await _process_download_notifications()

    rows = (
        await db_session.execute(select(DownloadNotification))
    ).scalars().all()
    assert len(rows) == 1
    assert await _deliveries(db_session, rows[0].id) == []


async def test_scheduler_tick_disabled(db_session, seed, monkeypatch):
    from app.services.scheduler import _process_download_notifications

    monkeypatch.setattr(settings, "notify_enabled", False)
    await _process_download_notifications()
    assert (
        await db_session.execute(select(DownloadNotification))
    ).scalars().all() == []


async def test_scheduler_tick_plans_orphan_notifications(db_session, seed):
    """tick 兜底补扫：已存在但没有任何计划行的通知（旧版本规划失败未落
    计划 / 规划被意外中断）在后续 tick 被补做规划。"""
    from app.models.library import Library
    from app.models.organize_plan import OrganizePlan
    from app.models.organize_rule import OrganizeRule
    from app.services.scheduler import _process_download_notifications

    # 先造一条孤儿通知：此时无 organize 规则，通知存在但绝无计划行。
    notification, created = await create_notification_for_task(db_session, seed.task)
    assert created
    await db_session.commit()

    # 补上启用的 organize 规则后跑 tick：补扫应捡起草儿通知并落计划
    # （测试环境磁盘无文件 → 确定性拒绝落 failed 计划行，关键是有行）。
    library = Library(id=_uuid(), name="TV", kind="tv")
    rule = OrganizeRule(
        id=_uuid(), name="tv", priority=100, enabled=True, filter=None,
        library_id=library.id, path_template="{title}/S{season:02d}",
        file_op="move", auto_execute=False,
    )
    db_session.add_all([library, rule])
    await db_session.commit()

    await _process_download_notifications()

    plans = (
        await db_session.execute(select(OrganizePlan))
    ).scalars().all()
    assert len(plans) == 1
    assert plans[0].notification_id == notification.id
    assert plans[0].status == "failed"

    # 再跑 tick：failed 计划已有行、快照未变 → 不重复重建。
    await _process_download_notifications()
    plans2 = (
        await db_session.execute(select(OrganizePlan))
    ).scalars().all()
    assert len(plans2) == 1
    actions = [a.action for a in plans2[0].audit_entries]
    assert actions.count("plan_failed") == 1


async def test_create_notification_survives_concurrent_race(db_session, seed, monkeypatch):
    """回归：regenerate/调度 tick 竞态——预检通过（无行）后、插入前，并发写
    入者已提交同一 download_task_id 的通知。唯一约束冲突必须被 SAVEPOINT
    捕获并回读已存在行，而不是炸掉整个请求。"""
    # 预置“并发写入者”已提交的行。
    await create_notification_for_task(db_session, seed.task)
    await db_session.commit()

    # 让第一次预检模拟“过期读”（并发行尚未可见），第二次走真实查询。
    real_find = notify_service._find_by_task
    calls = 0

    async def stale_find(db, task_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return await real_find(db, task_id)

    monkeypatch.setattr(notify_service, "_find_by_task", stale_find)

    n, created = await create_notification_for_task(db_session, seed.task)
    assert created is False
    assert n.download_task_id == seed.task.id
    # 会话仍然可用，且库里只有一行。
    await db_session.commit()
    rows = (await db_session.execute(select(DownloadNotification))).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# _cleanup_expired: notification retention & task protection
# ---------------------------------------------------------------------------


async def test_cleanup_expired_notification_retention(db_session, seed):
    """Notifications older than notify_retention_days are deleted (deliveries
    cascade); recent ones survive."""
    from app.services.scheduler import _cleanup_expired

    webhook = _webhook(seed.agent.id, mock=True, url="http://unused/x")
    db_session.add(webhook)
    old_n = DownloadNotification(
        id=_uuid(), agent_id=seed.agent.id, download_task_id=seed.task.id,
        payload={"notification_id": "old"},
        created_at=utcnow() - timedelta(days=settings.notify_retention_days + 1),
    )
    db_session.add(old_n)
    await db_session.flush()
    old_d = WebhookDelivery(
        id=_uuid(), notification_id=old_n.id, webhook_id=webhook.id,
        status="done", delivered_at=utcnow() - timedelta(days=31),
    )
    db_session.add(old_d)
    await db_session.commit()

    await _cleanup_expired()

    assert (
        await db_session.execute(select(DownloadNotification))
    ).scalars().all() == []
    assert (await db_session.execute(select(WebhookDelivery))).scalars().all() == []


async def test_cleanup_expired_protects_tasks_with_open_deliveries(
    db_session, seed
):
    """Expired completed tasks are deleted only when their notification has
    no non-done delivery."""
    from app.services.scheduler import _cleanup_expired

    # Make the task old enough to expire (agent default task_expire_days=30).
    seed.task.completed_at = utcnow() - timedelta(days=60)
    webhook = _webhook(seed.agent.id)
    db_session.add(webhook)
    n, _ = await create_notification_for_task(db_session, seed.task)
    n.created_at = utcnow()  # recent: retention does not apply
    await db_session.commit()
    await ensure_deliveries(db_session)

    # Pending delivery → task survives.
    await _cleanup_expired()
    remaining = (
        await db_session.execute(
            select(DownloadTask).where(DownloadTask.id == seed.task.id)
        )
    ).scalar_one_or_none()
    assert remaining is not None

    # All deliveries done → the task is cleaned up.
    for d in await _deliveries(db_session, n.id):
        d.status = "done"
        d.delivered_at = utcnow()
    await db_session.commit()
    await _cleanup_expired()
    remaining = (
        await db_session.execute(
            select(DownloadTask).where(DownloadTask.id == seed.task.id)
        )
    ).scalar_one_or_none()
    assert remaining is None
