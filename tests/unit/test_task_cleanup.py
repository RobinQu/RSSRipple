"""Tests for the organize post-action task cleanup helpers.

Covers the ``move`` path (``delete_task_after_organize``), the
``hardlink``/``copy`` preserve path (``resume_task_after_organize``) and the
``delete_data=true`` manual entry (``delete_task_with_data``). All three are
shared with ``DELETE /api/v1/tasks/{id}`` and the plan-cancel flow.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.services import task_cleanup as tc


def _uuid() -> str:
    return str(uuid.uuid4())


async def _make_task(
    db_session,
    *,
    transmission_torrent_id: int | None = 42,
    downloader_id: str | None = None,
    status: str = "completed",
) -> DownloadTask:
    from app.models.channel import Channel

    channel = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        field_mapping={"list_locator": {"source": "entries"},
                       "field_mappings": {"torrent_url": {"source": "link"}}},
        metadata_agent_enabled=False,
    )
    db_session.add(channel)
    await db_session.flush()
    resource = FileResource(
        id=_uuid(), channel_id=channel.id, guid=_uuid(), title_raw="raw",
        torrent_url="magnet:?xt=urn:btih:abc",
    )
    db_session.add(resource)
    await db_session.flush()
    task = DownloadTask(
        id=_uuid(),
        file_resource_id=resource.id,
        downloader_id=downloader_id,
        download_dir="/downloads/rssripple",
        transmission_torrent_id=transmission_torrent_id,
        status=status,
    )
    db_session.add(task)
    await db_session.commit()
    return task


@pytest.mark.asyncio
async def test_delete_task_after_organize_missing_task_is_idempotent_success(
    db_session,
):
    assert await tc.delete_task_after_organize(db_session, "does-not-exist") is True


@pytest.mark.asyncio
async def test_delete_task_after_organize_without_torrent_just_cancels(
    db_session, sample_downloader
):
    task = await _make_task(
        db_session, transmission_torrent_id=None, downloader_id=sample_downloader.id,
    )

    assert await tc.delete_task_after_organize(db_session, task.id) is True
    assert task.status == "cancelled"


@pytest.mark.asyncio
async def test_delete_task_after_organize_removes_torrent_keep_data(
    db_session, sample_downloader
):
    task = await _make_task(db_session, downloader_id=sample_downloader.id)

    wrapper = MagicMock()
    wrapper.remove_torrent = AsyncMock(return_value=True)
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        ok = await tc.delete_task_after_organize(db_session, task.id)

    assert ok is True
    assert task.status == "cancelled"
    wrapper.remove_torrent.assert_awaited_once_with(42, delete_data=False)


@pytest.mark.asyncio
async def test_delete_task_after_organize_rpc_failure_records_error(
    db_session, sample_downloader
):
    task = await _make_task(db_session, downloader_id=sample_downloader.id)

    wrapper = MagicMock()
    wrapper.remove_torrent = AsyncMock(side_effect=RuntimeError("rpc down"))
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        ok = await tc.delete_task_after_organize(db_session, task.id)

    assert ok is False
    assert task.status == "cancelled"
    assert task.error_message is not None


@pytest.mark.asyncio
async def test_delete_task_after_organize_missing_downloader_ok(
    db_session, sample_downloader
):
    """A downloader row that no longer resolves is treated as best-effort: the
    task is still cancelled and the result reports success."""
    from app.models.downloader import DownloaderInstance

    task = await _make_task(db_session, downloader_id=sample_downloader.id)
    real_get = AsyncMock(wraps=db_session.get)

    async def _ghost_get(model, pk, *a, **kw):
        if model is DownloaderInstance:
            return None
        return await real_get(model, pk, *a, **kw)

    db_session.get = _ghost_get  # type: ignore[method-assign]
    try:
        assert await tc.delete_task_after_organize(db_session, task.id) is True
    finally:
        db_session.get = real_get._mock_wraps  # type: ignore[method-assign]
    assert task.status == "cancelled"


@pytest.mark.asyncio
async def test_delete_task_with_data_missing_is_idempotent(db_session):
    assert await tc.delete_task_with_data(db_session, "nope") is True


@pytest.mark.asyncio
async def test_delete_task_with_data_removes_torrent_and_data(
    db_session, sample_downloader
):
    task = await _make_task(db_session, downloader_id=sample_downloader.id)

    wrapper = MagicMock()
    wrapper.remove_torrent = AsyncMock(return_value=True)
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        ok = await tc.delete_task_with_data(db_session, task.id)

    assert ok is True
    assert task.status == "cancelled"
    wrapper.remove_torrent.assert_awaited_once_with(42, delete_data=True)


@pytest.mark.asyncio
async def test_delete_task_with_data_rpc_failure(db_session, sample_downloader):
    task = await _make_task(db_session, downloader_id=sample_downloader.id)

    wrapper = MagicMock()
    wrapper.remove_torrent = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        ok = await tc.delete_task_with_data(db_session, task.id)

    assert ok is False
    assert task.status == "cancelled"
    assert task.error_message is not None


@pytest.mark.asyncio
async def test_resume_task_after_organize_missing_is_idempotent(db_session):
    assert await tc.resume_task_after_organize(db_session, "nope") is True


@pytest.mark.asyncio
async def test_resume_task_after_organize_without_torrent_is_noop(
    db_session, sample_downloader
):
    task = await _make_task(
        db_session, transmission_torrent_id=None, downloader_id=sample_downloader.id,
    )

    assert await tc.resume_task_after_organize(db_session, task.id) is True


@pytest.mark.asyncio
async def test_resume_task_after_organize_missing_downloader_false(
    db_session, sample_downloader
):
    from app.models.downloader import DownloaderInstance

    task = await _make_task(db_session, downloader_id=sample_downloader.id)
    real_get = AsyncMock(wraps=db_session.get)

    async def _ghost_get(model, pk, *a, **kw):
        if model is DownloaderInstance:
            return None
        return await real_get(model, pk, *a, **kw)

    db_session.get = _ghost_get  # type: ignore[method-assign]
    try:
        assert await tc.resume_task_after_organize(db_session, task.id) is False
    finally:
        db_session.get = real_get._mock_wraps  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_resume_task_after_organize_resumes_torrent(
    db_session, sample_downloader
):
    task = await _make_task(db_session, downloader_id=sample_downloader.id)

    wrapper = MagicMock()
    wrapper.resume_torrent = AsyncMock(return_value=True)
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        ok = await tc.resume_task_after_organize(db_session, task.id)

    assert ok is True
    wrapper.resume_torrent.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_resume_task_after_organize_rpc_failure(db_session, sample_downloader):
    task = await _make_task(db_session, downloader_id=sample_downloader.id)

    wrapper = MagicMock()
    wrapper.resume_torrent = AsyncMock(side_effect=RuntimeError("down"))
    with patch("app.clients.transmission.TransmissionWrapper", return_value=wrapper):
        ok = await tc.resume_task_after_organize(db_session, task.id)

    assert ok is False
