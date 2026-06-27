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


@pytest.fixture
async def _seed(db_session):
    """Create a channel, downloader, agent, and a couple of resources/tasks."""
    ch = Channel(id=_uuid(), name="ch", type="rss_feed", url="https://x/rss",
                 metadata_source="none", title_extraction_method="none")
    dl = DownloaderInstance(
        id=_uuid(), name="dl", type="transmission",
        url="http://127.0.0.1:9091/transmission/rpc", download_dir="/tmp",
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
        transmission_torrent_id=42, status="downloading", progress=0.1,
    )
    t2 = DownloadTask(
        id=_uuid(), agent_id=agent.id, file_resource_id=r2.id, downloader_id=dl.id,
        transmission_torrent_id=43, status="downloading", progress=0.2,
    )
    t_done = DownloadTask(
        id=_uuid(), agent_id=agent.id, file_resource_id=r1.id, downloader_id=dl.id,
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
async def test_sync_download_progress_transmission_error_marks_error(db_session, _seed, monkeypatch):
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
    await db_session.refresh(_seed.dl)
    assert _seed.t1.status == "error"
    assert _seed.dl.status == "error"


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
    from sqlalchemy import select, func
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
