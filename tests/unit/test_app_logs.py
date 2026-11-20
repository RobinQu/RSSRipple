"""Persistent external-dependency logs and manual retry tests."""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.config import settings
from app.models.app_log import AppLog
from app.models.file_resource import FileResource
from app.services.app_logs import (
    cleanup_app_logs,
    record_external_failure,
    resolve_external_logs,
    retry_external_failure,
)
from app.utils.time import utcnow


def _uuid():
    return str(uuid.uuid4())


async def _resource(db_session, channel):
    row = FileResource(
        id=_uuid(), channel_id=channel.id, guid=_uuid(), title_raw="Show - 01",
        torrent_url="https://example.com/show.torrent",
        metadata_attempts=settings.metadata_transient_max_attempts,
        metadata_failure_type="transient",
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def test_failure_dedup_resolve_and_retention(db_session, sample_channel):
    resource = await _resource(db_session, sample_channel)
    first = await record_external_failure(
        db_session, category="metadata", message="wigolo timeout",
        resource_id=resource.id, channel_id=sample_channel.id, attempts=5,
    )
    second = await record_external_failure(
        db_session, category="metadata", message="wigolo disconnected",
        resource_id=resource.id, channel_id=sample_channel.id, attempts=6,
    )
    await db_session.commit()

    assert first.id == second.id
    assert second.message == "wigolo disconnected"
    assert (await db_session.execute(select(AppLog))).scalars().all() == [second]

    assert await resolve_external_logs(db_session, resource.id, ["metadata"]) == 1
    second.updated_at = utcnow() - timedelta(days=settings.app_log_retention_days + 1)
    await db_session.commit()
    assert await cleanup_app_logs(db_session) == 1


async def test_manual_metadata_retry_resets_budget_and_creates_durable_request(
    db_session, sample_channel, monkeypatch,
):
    resource = await _resource(db_session, sample_channel)
    log = await record_external_failure(
        db_session, category="metadata", message="wigolo timeout",
        resource_id=resource.id, channel_id=sample_channel.id, attempts=5,
    )
    wake = AsyncMock()
    monkeypatch.setattr("app.services.resource_reparse_requests.wake_request", wake)

    await retry_external_failure(db_session, log)

    assert log.status == "retrying" and log.retry_count == 1
    assert resource.metadata_attempts == 0
    assert resource.metadata_failure_type is None
    wake.assert_awaited_once()
    assert wake.await_args.args[0].resource_id == resource.id


async def test_http_exhaustion_logs_and_success_resolves(db_session, sample_channel):
    from app.services.resource_listing import (
        claim_torrent_fetch,
        record_torrent_fetch_failure,
        record_torrent_fetch_success,
    )

    resource = await _resource(db_session, sample_channel)
    attempt = await claim_torrent_fetch(db_session, resource.id)
    await db_session.commit()
    assert await record_torrent_fetch_failure(
        db_session, resource.id, attempt, "destination denied", retryable=False,
    )
    log = await db_session.scalar(select(AppLog).where(AppLog.resource_id == resource.id))
    assert log.category == "torrent" and log.status == "failed"

    retry = await claim_torrent_fetch(db_session, resource.id, force=True)
    await db_session.commit()
    assert await record_torrent_fetch_success(
        db_session, resource.id, retry, "/tmp/show.torrent", expected_torrent_file=None,
    )
    await db_session.refresh(log)
    assert log.status == "resolved" and log.resolved_at is not None


async def test_metadata_failure_message_preserves_root_cause():
    from types import SimpleNamespace

    from app.services.fetch_service import _metadata_failure_message

    result = SimpleNamespace(
        search_error="WigoloSearchError: name resolution failed",
        reason="fallback failed",
        source_errors={"tmdb": "incomplete search", "wigolo": "ReadError"},
    )
    message = _metadata_failure_message(SimpleNamespace(), result)
    assert "name resolution failed" in message
    assert "tmdb: incomplete search" in message
    assert "wigolo: ReadError" in message
