"""In-process coverage for ``app.services.task_cleanup``.

Covers the branches the HTTP organize suite does not reach: idempotent
success on a missing task, RPC failure handling (error recorded, task still
cancelled, ``False`` returned), ``delete_task_with_data`` end to end, and the
``resume_task_after_organize`` early-exit / error paths.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.services.task_cleanup import (
    delete_task_after_organize,
    delete_task_with_data,
    resume_task_after_organize,
)


def _uuid() -> str:
    return str(uuid.uuid4())


async def _make_task(
    db,
    *,
    transmission_torrent_id: int | None = 7,
    with_downloader: bool = True,
) -> DownloadTask:
    """Persist the minimal Channel → FileResource → DownloaderInstance →
    DownloadTask chain that a DownloadTask row requires."""
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    channel = Channel(
        id=_uuid(),
        name="cleanup-channel",
        type="rss_feed",
        url="https://example.com/rss",
        fetch_interval=1800,
        status="active",
        field_mapping={
            "list_locator": {"source": "entries"},
            "field_mappings": {"torrent_url": {"source": "link"}},
        },
        metadata_agent_enabled=False,
    )
    db.add(channel)
    downloader = None
    if with_downloader:
        downloader = DownloaderInstance(
            id=_uuid(),
            name="cleanup-downloader",
            type="transmission",
            url="http://127.0.0.1:9091/transmission/rpc",
            download_dir="/downloads",
            status="connected",
        )
        db.add(downloader)
    await db.flush()
    resource = FileResource(
        id=_uuid(),
        channel_id=channel.id,
        guid=_uuid(),
        title_raw="raw",
        torrent_url="magnet:?xt=urn:btih:abc",
    )
    db.add(resource)
    await db.flush()
    task = DownloadTask(
        id=_uuid(),
        agent_id=None,
        file_resource_id=resource.id,
        downloader_id=downloader.id if downloader else _uuid(),
        download_dir="/downloads",
        transmission_torrent_id=transmission_torrent_id,
        status="completed",
    )
    db.add(task)
    await db.flush()
    return task


@pytest.fixture
def rpc(monkeypatch):
    """Patch the downloader client factory used inside task_cleanup."""
    wrapper = SimpleNamespace(
        remove_torrent=AsyncMock(return_value=True),
        resume_torrent=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.task_cleanup.get_downloader_client", lambda _dl: wrapper
    )
    return wrapper


# ── delete_task_after_organize ──────────────────────────────────────────────


async def test_delete_task_after_organize_missing_task_is_idempotent(db_session, rpc):
    assert await delete_task_after_organize(db_session, _uuid()) is True
    rpc.remove_torrent.assert_not_called()


async def test_delete_task_after_organize_success(db_session, rpc):
    task = await _make_task(db_session)
    assert await delete_task_after_organize(db_session, task.id) is True
    rpc.remove_torrent.assert_awaited_once_with(7, delete_data=False)
    assert task.status == "cancelled"
    assert task.error_message is None


async def test_delete_task_after_organize_rpc_failure(db_session, rpc):
    task = await _make_task(db_session)
    rpc.remove_torrent.side_effect = RuntimeError("rpc down")
    assert await delete_task_after_organize(db_session, task.id) is False
    # RPC failure still cancels the row and records the error for the UI.
    assert task.status == "cancelled"
    assert task.error_message == "rpc down"


async def test_delete_task_after_organize_without_torrent_skips_rpc(db_session, rpc):
    task = await _make_task(db_session, transmission_torrent_id=None)
    assert await delete_task_after_organize(db_session, task.id) is True
    rpc.remove_torrent.assert_not_called()
    assert task.status == "cancelled"


# ── delete_task_with_data ───────────────────────────────────────────────────


async def test_delete_task_with_data_missing_task_is_idempotent(db_session, rpc):
    assert await delete_task_with_data(db_session, _uuid()) is True
    rpc.remove_torrent.assert_not_called()


async def test_delete_task_with_data_success(db_session, rpc):
    task = await _make_task(db_session)
    assert await delete_task_with_data(db_session, task.id) is True
    rpc.remove_torrent.assert_awaited_once_with(7, delete_data=True)
    assert task.status == "cancelled"


async def test_delete_task_with_data_rpc_failure(db_session, rpc):
    task = await _make_task(db_session)
    rpc.remove_torrent.side_effect = RuntimeError("disk busy")
    assert await delete_task_with_data(db_session, task.id) is False
    assert task.status == "cancelled"
    assert task.error_message == "disk busy"


# ── resume_task_after_organize ──────────────────────────────────────────────


async def test_resume_missing_task_is_idempotent(db_session, rpc):
    assert await resume_task_after_organize(db_session, _uuid()) is True
    rpc.resume_torrent.assert_not_called()


async def test_resume_without_torrent_is_noop(db_session, rpc):
    task = await _make_task(db_session, transmission_torrent_id=None)
    assert await resume_task_after_organize(db_session, task.id) is True
    rpc.resume_torrent.assert_not_called()


async def test_resume_success(db_session, rpc):
    task = await _make_task(db_session)
    assert await resume_task_after_organize(db_session, task.id) is True
    rpc.resume_torrent.assert_awaited_once_with(7)
    # Resume never rewrites the task row status.
    assert task.status == "completed"


async def test_resume_rpc_failure(db_session, rpc):
    task = await _make_task(db_session)
    rpc.resume_torrent.side_effect = RuntimeError("rpc down")
    assert await resume_task_after_organize(db_session, task.id) is False
    assert task.status == "completed"


async def test_resume_downloader_row_gone(db_session):
    """The FK makes a dangling downloader impossible on a real session, so
    exercise the branch with a stub session that returns the task but no
    DownloaderInstance row."""
    task = SimpleNamespace(
        transmission_torrent_id=7, downloader_id=_uuid()
    )

    class _StubDB:
        async def get(self, model, key):
            if model is DownloadTask:
                return task
            return None

    assert await resume_task_after_organize(_StubDB(), "task-id") is False
