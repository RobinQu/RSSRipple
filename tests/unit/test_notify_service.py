"""Unit tests for app.services.notify_service.

Covers the pure payload builder, notification creation (idempotency, torrent
pause + file-listing snapshot), webhook delivery (mock / unregistered /
success / backoff exhaustion), and backfill. Downloader RPC is exercised
through the mock downloader; HTTP delivery is stubbed at httpx.AsyncClient.
"""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.services import notify_service
from app.services.notify_service import (
    backfill_notifications,
    backoff_delay,
    build_payload,
    create_notification_for_task,
    deliver_due_notifications,
    reset_for_retry,
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
        collection=SimpleNamespace(display_name="Frieren"),
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
    assert work["collection"] == "Frieren"
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
        collection=None,
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
    assert work["collection"] is None
    assert work["episodes"] is None


def test_build_payload_without_torrent_info():
    payload = build_payload(
        "notif-3", None, SimpleNamespace(id="t", download_dir="/d"),
        _resource_ns(), None,
    )
    assert "files" not in payload
    assert payload["task"]["torrent_name"] is None


def test_backoff_delay_grows_and_caps(monkeypatch):
    monkeypatch.setattr(settings, "notify_retry_base_seconds", 30)
    assert backoff_delay(1).total_seconds() == 60
    assert backoff_delay(2).total_seconds() == 120
    assert backoff_delay(20).total_seconds() == notify_service.MAX_BACKOFF_SECONDS


# ---------------------------------------------------------------------------
# DB-backed tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def seed(db_session):
    """Channel + mock downloader + agent + series + resource + completed task.

    Also registers a torrent in the mock downloader store so the file-listing
    RPC path returns real data.
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


async def test_create_notification_snapshots_and_pauses(db_session, seed):
    n, created = await create_notification_for_task(db_session, seed.task)
    await db_session.commit()

    assert n.status == "pending"
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


async def test_deliver_mock_webhook_succeeds_without_http(db_session, seed):
    seed.agent.notify_webhook_mock = True
    await create_notification_for_task(db_session, seed.task)
    await db_session.commit()

    stats = await deliver_due_notifications(db_session)
    assert stats["delivered"] == 1
    assert stats["failed"] == 0
    n = (
        await db_session.execute(
            __import__("sqlalchemy").select(DownloadNotification)
        )
    ).scalar_one()
    assert n.notified_at is not None
    assert n.status == "pending"  # delivered, awaiting consumer callbacks


async def test_deliver_unregistered_webhook_waits(db_session, seed):
    await create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    stats = await deliver_due_notifications(db_session)
    assert stats["delivered"] == 0
    assert stats["skipped"] == 1


class _FakeResp:
    def raise_for_status(self):
        return None


def _stub_httpx(monkeypatch, calls: list, fail: bool = False):
    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append((url, json))
            if fail:
                raise httpx_error()
            return _FakeResp()

    def httpx_error():
        import httpx

        return httpx.ConnectError("connection refused")

    monkeypatch.setattr(notify_service.httpx, "AsyncClient", _FakeClient)


async def test_deliver_http_success(db_session, seed, monkeypatch):
    seed.agent.notify_webhook_url = "http://organizer:8910/webhook"
    await create_notification_for_task(db_session, seed.task)
    await db_session.commit()

    calls: list = []
    _stub_httpx(monkeypatch, calls)
    stats = await deliver_due_notifications(db_session)

    assert stats["delivered"] == 1
    assert len(calls) == 1
    url, body = calls[0]
    assert url == "http://organizer:8910/webhook"
    assert body["event"] == "download.completed"
    assert body["notification"]["notification_id"]


async def test_deliver_failure_backoff_then_failed(
    db_session, seed, monkeypatch
):
    monkeypatch.setattr(settings, "notify_max_attempts", 2)
    seed.agent.notify_webhook_url = "http://organizer:8910/webhook"
    n, created = await create_notification_for_task(db_session, seed.task)
    await db_session.commit()

    calls: list = []
    _stub_httpx(monkeypatch, calls, fail=True)

    stats = await deliver_due_notifications(db_session)
    assert stats["failed"] == 0
    assert n.attempt_count == 1
    assert n.status == "pending"
    assert n.next_attempt_at is not None

    # Force the row due again and let the second failure exhaust attempts.
    n.next_attempt_at = utcnow()
    await db_session.commit()
    stats = await deliver_due_notifications(db_session)
    assert stats["failed"] == 1
    assert n.status == "failed"
    assert "最大重试次数" in (n.error_message or "")


async def test_backfill_only_missing_and_since_filter(db_session, seed):
    from sqlalchemy import select

    # Task 1 already has a notification — must be skipped by backfill.
    await create_notification_for_task(db_session, seed.task)

    # Task 2: older completed task without a notification.
    older = DownloadTask(
        id=_uuid(), agent_id=seed.agent.id,
        file_resource_id=seed.resource.id, downloader_id=seed.downloader.id,
        download_dir="/downloads/rssripple",
        transmission_torrent_id=None, status="completed",
        completed_at=utcnow().replace(year=2020),
    )
    db_session.add(older)
    await db_session.commit()

    created = await backfill_notifications(db_session, seed.agent.id, None)
    assert created == 1  # only the older task

    # since after the older task → nothing left to backfill.
    created = await backfill_notifications(db_session, seed.agent.id, utcnow())
    assert created == 0

    rows = (await db_session.execute(select(DownloadNotification))).scalars().all()
    assert len(rows) == 2


async def test_reset_for_retry():
    n = SimpleNamespace(status="failed", attempt_count=5,
                        next_attempt_at=None, error_message="x")
    reset_for_retry(n)
    assert n.status == "pending"
    assert n.attempt_count == 0
    assert n.next_attempt_at is not None
    assert n.error_message is None


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
    assert n.status == "pending"
    assert "files" not in n.payload
    assert n.payload["task"]["torrent_name"] is None


async def test_deliver_skips_notification_without_agent(db_session, seed):
    n, created = await create_notification_for_task(db_session, seed.task)
    n.agent_id = None  # Agent 被删除（SET NULL）后的历史通知
    await db_session.commit()
    stats = await deliver_due_notifications(db_session)
    assert stats["delivered"] == 0
    assert stats["skipped"] == 1


async def test_backfill_batches_commits(db_session, seed):
    """超过批次大小的 backfill 正常分批提交。"""
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
    created = await backfill_notifications(db_session, seed.agent.id, None)
    # 25 个新任务 + seed 自带的 1 个 completed 任务
    assert created == 26


async def test_scheduler_tick_enqueues_and_delivers(db_session, seed):
    """_process_download_notifications：为 completed 任务补建通知并投递
    （mock webhook 直接成功）。"""
    from app.services.scheduler import _process_download_notifications

    seed.agent.notify_webhook_mock = True
    await db_session.commit()
    # seed 的任务尚无通知（该 fixture 不创建通知行）
    from sqlalchemy import select

    assert (
        await db_session.execute(select(DownloadNotification))
    ).scalars().all() == []

    await _process_download_notifications()

    rows = (
        await db_session.execute(select(DownloadNotification))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].notified_at is not None  # mock 投递成功
    assert rows[0].payload["work"]["title_en"] == "Test Series"


async def test_scheduler_tick_disabled(db_session, seed, monkeypatch):
    from app.services.scheduler import _process_download_notifications

    monkeypatch.setattr(settings, "notify_enabled", False)
    await _process_download_notifications()
    from sqlalchemy import select

    assert (
        await db_session.execute(select(DownloadNotification))
    ).scalars().all() == []


async def test_create_notification_survives_concurrent_race(db_session, seed, monkeypatch):
    """回归：backfill/调度 tick 竞态——预检通过（无行）后、插入前，并发写
    入者已提交同一 download_task_id 的通知。唯一约束冲突必须被 SAVEPOINT
    捕获并回读已存在行，而不是炸掉整个请求。"""
    from sqlalchemy import select as sa_select

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
    rows = (await db_session.execute(sa_select(DownloadNotification))).scalars().all()
    assert len(rows) == 1
