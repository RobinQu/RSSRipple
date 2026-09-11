"""Unit tests for the ``reprocess_resource_metadata`` background job.

The endpoint sets ``confirmation_ignored_at`` up front so the resource
leaves the dashboard todo list immediately; the job clears the flag when it
finishes — success or failure — so the confirmation policy re-evaluates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.job_handlers import _handle_reprocess_resource_metadata
from app.models.file_resource import FileResource


def _uuid() -> str:
    return str(uuid.uuid4())


async def _make_resource(session, channel_id, **overrides) -> str:
    defaults = dict(
        id=_uuid(),
        channel_id=channel_id,
        guid=_uuid(),
        title_raw="[Group] Show - 01 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
        search_title="Show",
        parsed_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    session.add(FileResource(**defaults))
    await session.commit()
    return defaults["id"]


def _patch_env(monkeypatch, process: AsyncMock) -> None:
    monkeypatch.setattr("app.job_handlers._refresh_runtime_config", AsyncMock())
    monkeypatch.setattr(
        "app.services.fetch_service._process_resource_metadata", process
    )


@pytest.mark.asyncio
async def test_clears_confirmation_ignore_on_success(
    monkeypatch, db_session, sample_channel,
):
    process = AsyncMock()
    _patch_env(monkeypatch, process)
    rid = await _make_resource(
        db_session, sample_channel.id,
        confirmation_ignored_at=datetime.now(UTC),
    )

    result = await _handle_reprocess_resource_metadata(
        {"resource_id": rid, "channel_id": sample_channel.id}
    )

    assert result["status"] == "done"
    args, kwargs = process.await_args
    assert args[0] == rid
    assert args[1] == sample_channel.id
    assert kwargs["force_refresh"] is True
    db_session.expire_all()
    r = await db_session.get(FileResource, rid)
    assert r.confirmation_ignored_at is None


@pytest.mark.asyncio
async def test_clears_confirmation_ignore_on_failure(
    monkeypatch, db_session, sample_channel,
):
    _patch_env(monkeypatch, AsyncMock(side_effect=RuntimeError("boom")))
    rid = await _make_resource(
        db_session, sample_channel.id,
        confirmation_ignored_at=datetime.now(UTC),
    )

    with pytest.raises(RuntimeError):
        await _handle_reprocess_resource_metadata(
            {"resource_id": rid, "channel_id": sample_channel.id}
        )

    db_session.expire_all()
    r = await db_session.get(FileResource, rid)
    assert r.confirmation_ignored_at is None
