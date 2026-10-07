"""Actual API/DB/handler lifecycle; synthetic metadata work is labelled explicitly."""

import socket
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update

from app.api.v1.dashboard import _page_pending_confirmations
from app.job_handlers import _handle_reprocess_resource_metadata
from app.models.file_resource import FileResource
from app.models.resource_reparse_request import ResourceReparseRequest
from app.services.resource_reparse_requests import (
    create_request,
    dispatch_pending_reparses,
    finish_request,
)
from app.services.task_queue import MemoryQueue, RedisQueue
from app.utils.time import utcnow
from tests.api.test_resources import _make_resource


async def _visible(factory, rid):
    async with factory() as db:
        items, _ = await _page_pending_confirmations(db, 1, 100)
        return any(item["resource"]["id"] == rid for item in items)


@pytest.mark.parametrize("manual_timing", ["before", "during", "none"])
@pytest.mark.parametrize("pipeline_failed", [False, True])
async def test_reparse_acknowledges_own_request_without_clearing_manual_ignore(
    client, sample_channel, db_session_factory, monkeypatch, manual_timing, pipeline_failed,
):
    rid = await _make_resource(db_session_factory, sample_channel.id)
    monkeypatch.setattr("app.database.async_session_factory", db_session_factory)
    monkeypatch.setattr("app.job_handlers._refresh_runtime_config", AsyncMock())
    enqueue = AsyncMock(return_value={"job_id": "synthetic"})
    monkeypatch.setattr("app.services.task_queue.task_queue.enqueue", enqueue)

    async def ignore():
        response = await client.post("/api/v1/dashboard/todos/ignore", json={"kind": "confirmation", "ids": [rid]})
        assert response.status_code == 200

    if manual_timing == "before":
        await ignore()
    response = await client.post(f"/api/v1/resources/{rid}/reparse-metadata")
    assert response.status_code == 200
    assert not await _visible(db_session_factory, rid)
    payload = enqueue.call_args.args[2]

    async def metadata_pipeline(*args, **kwargs):
        if manual_timing == "during":
            await ignore()
        if pipeline_failed:
            raise ValueError("Synthetic metadata failure")

    monkeypatch.setattr("app.services.fetch_service._process_resource_metadata", metadata_pipeline)
    if pipeline_failed:
        with pytest.raises(ValueError, match="Synthetic"):
            await _handle_reprocess_resource_metadata(payload)
    else:
        assert await _handle_reprocess_resource_metadata(payload) == {"status": "done"}
    async with db_session_factory() as db:
        assert await db.get(ResourceReparseRequest, payload["request_id"]) is None
        ignored = (await db.get(FileResource, rid)).confirmation_ignored_at is not None
    assert ignored == (manual_timing != "none")
    assert await _visible(db_session_factory, rid) == (manual_timing == "none")


async def test_real_connection_refusal_leaves_visible_durable_request_then_sweep_delivers(
    client, sample_channel, db_session_factory, monkeypatch,
):
    rid = await _make_resource(db_session_factory, sample_channel.id)
    monkeypatch.setattr("app.database.async_session_factory", db_session_factory)
    assert await _visible(db_session_factory, rid)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        redis = Redis(host="127.0.0.1", port=reserved.getsockname()[1], socket_connect_timeout=0.2)
        queue = RedisQueue(redis_client=redis)
        await queue.start(consume=False)
        monkeypatch.setattr("app.services.task_queue.task_queue", queue)
        try:
            response = await client.post(f"/api/v1/resources/{rid}/reparse-metadata")
            # DB accepted the durable request despite queue delivery failure.
            assert response.status_code == 200
            assert response.json()["data"]["reparse"] == {"status": "pending"}
        finally:
            await redis.aclose()
    assert await _visible(db_session_factory, rid)
    async with db_session_factory() as db:
        request = await db.scalar(select(ResourceReparseRequest).where(ResourceReparseRequest.resource_id == rid))
        request_id = request.id
        assert request.attempt_count == 1
        assert request.error_message == "Queue delivery unavailable"
        assert request.next_attempt_at > utcnow()
        request.next_attempt_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    recovered = MemoryQueue()
    await recovered.start(consume=False)
    monkeypatch.setattr("app.services.task_queue.task_queue", recovered)
    try:
        await dispatch_pending_reparses()
        state = await recovered.status(f"reprocess-resource:{rid}")
        assert state["status"] == "queued"
        async with db_session_factory() as db:
            request = await db.get(ResourceReparseRequest, request_id)
            assert request.error_message is None
        assert not await _visible(db_session_factory, rid)
    finally:
        await recovered.stop()


async def test_duplicate_keeps_request_identity_and_does_not_resubmit(
    client, sample_channel, db_session_factory, monkeypatch,
):
    rid = await _make_resource(db_session_factory, sample_channel.id)
    monkeypatch.setattr("app.database.async_session_factory", db_session_factory)
    observed = []

    async def enqueue(job_type, key, payload):
        async with db_session_factory() as db:
            assert await db.get(ResourceReparseRequest, payload["request_id"])
        observed.append(payload)
        return {"job_id": "synthetic"}

    monkeypatch.setattr("app.services.task_queue.task_queue.enqueue", enqueue)
    assert (await client.post(f"/api/v1/resources/{rid}/reparse-metadata")).status_code == 200
    assert (await client.post(f"/api/v1/resources/{rid}/reparse-metadata")).status_code == 409
    assert len(observed) == 1
    async with db_session_factory() as db:
        assert await db.get(ResourceReparseRequest, observed[0]["request_id"])


async def test_commit_without_enqueue_is_recovered_and_stale_ack_cannot_delete_new_request(
    sample_channel, db_session_factory, monkeypatch,
):
    rid = await _make_resource(db_session_factory, sample_channel.id)
    monkeypatch.setattr("app.database.async_session_factory", db_session_factory)
    async with db_session_factory() as db:
        old = await create_request(db, rid, sample_channel.id)
        await db.commit()  # Deliberately omit enqueue: durable crash-window boundary.
    async with db_session_factory() as db:
        await db.execute(update(ResourceReparseRequest).values(next_attempt_at=utcnow() - timedelta(seconds=1)))
        await db.commit()
    enqueue = AsyncMock(return_value={"job_id": "synthetic"})
    monkeypatch.setattr("app.services.task_queue.task_queue.enqueue", enqueue)
    await dispatch_pending_reparses()
    assert enqueue.call_args.args[2]["request_id"] == old.id
    async with db_session_factory() as db:
        await finish_request(db, old.id, rid)
        new = await create_request(db, rid, sample_channel.id)
        await db.commit()
    async with db_session_factory() as db:
        await finish_request(db, old.id, rid)
        await db.commit()
    process = AsyncMock()
    monkeypatch.setattr("app.services.fetch_service._process_resource_metadata", process)
    assert await _handle_reprocess_resource_metadata(enqueue.call_args.args[2]) == {"status": "superseded"}
    process.assert_not_awaited()
    async with db_session_factory() as db:
        assert await db.get(ResourceReparseRequest, new.id)


async def test_sweep_claims_at_most_fifty_without_starving_next_page(sample_channel, db_session_factory, monkeypatch):
    import uuid

    from app.services import resource_reparse_requests as service

    monkeypatch.setattr("app.database.async_session_factory", db_session_factory)
    now = utcnow()
    async with db_session_factory() as db:
        resources = [FileResource(
            id=str(uuid.uuid4()), channel_id=sample_channel.id, guid=str(i), title_raw="Synthetic bounded request",
            torrent_url="magnet:?xt=urn:btih:synthetic",
        ) for i in range(51)]
        db.add_all(resources)
        await db.flush()
        for resource in resources:
            await create_request(db, resource.id, sample_channel.id)
        await db.execute(update(ResourceReparseRequest).values(next_attempt_at=now - timedelta(seconds=1)))
        await db.commit()
    delivered = []

    async def observe_claim(request):
        delivered.append(request.id)
        async with db_session_factory() as db:
            # The claim really committed before the queue delivery boundary.
            assert (await db.get(ResourceReparseRequest, request.id)).next_attempt_at > now

    monkeypatch.setattr(service, "wake_request", observe_claim)
    await dispatch_pending_reparses()
    assert len(delivered) == len(set(delivered)) == 50
    await dispatch_pending_reparses()
    assert len(delivered) == len(set(delivered)) == 51


async def test_delivery_backoff_caps_and_never_persists_exception_secrets(
    sample_channel, db_session_factory, monkeypatch,
):
    from app.services import resource_reparse_requests as service

    monkeypatch.setattr("app.database.async_session_factory", db_session_factory)
    fixed = utcnow()
    monkeypatch.setattr(service, "utcnow", lambda: fixed)
    rid = await _make_resource(db_session_factory, sample_channel.id)
    async with db_session_factory() as db:
        request = await create_request(db, rid, sample_channel.id)
        await db.commit()
    enqueue = AsyncMock(side_effect=ConnectionError("redis://user:synthetic-secret@example.invalid"))
    monkeypatch.setattr("app.services.task_queue.task_queue.enqueue", enqueue)
    for count, delay in enumerate([30, 60, 120, 240, 480, 960, 1800, 1800], start=1):
        assert not await service.wake_request(request)
        async with db_session_factory() as db:
            saved = await db.get(ResourceReparseRequest, request.id)
            assert saved.attempt_count == count
            assert saved.next_attempt_at == fixed + timedelta(seconds=delay)
            assert saved.error_message == "Queue delivery unavailable"
    enqueue.side_effect = None
    enqueue.return_value = {"job_id": "synthetic"}
    assert await service.wake_request(request)
    async with db_session_factory() as db:
        saved = await db.get(ResourceReparseRequest, request.id)
        assert saved.error_message is None
        assert saved.next_attempt_at == fixed + timedelta(seconds=30)


async def test_ack_before_enqueue_returns_cannot_recreate_completed_request(
    sample_channel, db_session_factory, monkeypatch,
):
    from app.services import resource_reparse_requests as service

    monkeypatch.setattr("app.database.async_session_factory", db_session_factory)
    rid = await _make_resource(db_session_factory, sample_channel.id)
    async with db_session_factory() as db:
        request = await create_request(db, rid, sample_channel.id)
        await db.commit()

    async def complete_before_response(*args, **kwargs):
        async with db_session_factory() as db:
            await finish_request(db, request.id, rid)
            await db.commit()
        raise ConnectionError("Injected late response failure after acknowledgement")

    monkeypatch.setattr("app.services.task_queue.task_queue.enqueue", complete_before_response)
    assert not await service.wake_request(request)
    async with db_session_factory() as db:
        assert await db.get(ResourceReparseRequest, request.id) is None
