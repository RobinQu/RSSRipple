"""Real Redis producer/consumer ownership and API persistence; metadata is synthetic."""

import asyncio
import os
import uuid
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import select

from app.job_handlers import _handle_reprocess_resource_metadata
from app.models.file_resource import FileResource
from app.models.resource_reparse_request import ResourceReparseRequest
from app.services.task_queue import RedisQueue, current_job_identity, require_execution_ownership


@pytest.mark.parametrize("outcome", ["success", "response_lost", "metadata_error"])
async def test_real_redis_worker_acknowledges_only_its_durable_request(
    client, sample_channel, db_session_factory, monkeypatch, outcome, record_property,
):
    url = os.environ.get("REPARSE_TEST_REDIS_URL") or os.environ.get("QUEUE_RECOVERY_REDIS_URL")
    if not url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Reparse worker gate requires dedicated Redis")
        pytest.skip("Run isolated integration gate for real Redis worker")
    # Use a separate DB within the explicitly configured TEST Redis, without
    # flushing any data or touching other suites' queue DBs (0 and 1).
    url = urlunsplit(urlsplit(url)._replace(path="/14"))
    monkeypatch.setattr("app.database.async_session_factory", db_session_factory)
    monkeypatch.setattr("app.job_handlers._refresh_runtime_config", AsyncMock())
    rid = str(uuid.uuid4())
    async with db_session_factory() as db:
        db.add(FileResource(id=rid, channel_id=sample_channel.id, guid=rid,
                            title_raw="Synthetic reparse worker", torrent_url="magnet:?xt=urn:btih:synthetic"))
        await db.commit()
    producer, worker = RedisQueue(redis_url=url), RedisQueue(redis_url=url)
    entered, release = asyncio.Event(), asyncio.Event()
    identities = []

    async def metadata_pipeline(resource_id, channel_id, semaphore, **kwargs):
        assert resource_id == rid
        assert channel_id == sample_channel.id
        assert kwargs["force_refresh"] is True
        await require_execution_ownership()  # Actual Redis token/active/lease check.
        identities.append(current_job_identity())
        entered.set()
        await release.wait()
        await require_execution_ownership()
        if outcome == "metadata_error":
            raise ValueError("Synthetic metadata provider error")

    monkeypatch.setattr("app.services.fetch_service._process_resource_metadata", metadata_pipeline)
    monkeypatch.setattr("app.services.task_queue.task_queue", producer)
    worker.register("reprocess_resource_metadata", _handle_reprocess_resource_metadata)
    await producer.start(consume=False)
    await worker.start()
    original_enqueue = producer.enqueue

    async def enqueue_then_lose_response(*args, **kwargs):
        accepted = await original_enqueue(*args, **kwargs)
        assert accepted is not None
        raise ConnectionError("Injected response loss AFTER real Redis acceptance")

    if outcome == "response_lost":
        monkeypatch.setattr(producer, "enqueue", enqueue_then_lose_response)
    try:
        response = await client.post(f"/api/v1/resources/{rid}/reparse-metadata")
        assert response.status_code == 200
        await asyncio.wait_for(entered.wait(), timeout=10)
        state = await producer.status(f"reprocess-resource:{rid}")
        assert state["status"] == "running"
        assert identities == [(f"reprocess-resource:{rid}", state["job_id"])]
        async with db_session_factory() as db:
            pending = await db.scalar(select(ResourceReparseRequest).where(ResourceReparseRequest.resource_id == rid))
            request_id = pending.id
        # A real user action while the actual consumer is running must survive.
        ignored = await client.post("/api/v1/dashboard/todos/ignore", json={"kind": "confirmation", "ids": [rid]})
        assert ignored.status_code == 200
        release.set()
        async with asyncio.timeout(10):
            while True:
                state = await producer.status(f"reprocess-resource:{rid}")
                if state["status"] in {"done", "failed"}:
                    break
                await asyncio.sleep(0.02)
        assert state["status"] == ("failed" if outcome == "metadata_error" else "done")
        async with db_session_factory() as db:
            assert await db.get(ResourceReparseRequest, request_id) is None
            assert (await db.get(FileResource, rid)).confirmation_ignored_at is not None
        record_property("actual_redis_execution_ownership", True)
        record_property("metadata_provider", "synthetic blocked coroutine")
        record_property("response_loss", "injected after real enqueue" if outcome == "response_lost" else "none")
        record_property("queue_terminal_status", state["status"])
    finally:
        release.set()
        await worker.stop()
        # Delete only this case's known synthetic task keys, never FLUSHDB.
        await producer._redis.delete(f"rssripple:job:reprocess-resource:{rid}",
                                     f"rssripple:active:reprocess-resource:{rid}", worker._processing_key)
        await producer.stop()


async def test_worker_cancellation_requeues_and_preserves_request(
    client, sample_channel, db_session_factory, monkeypatch, record_property,
):
    url = os.environ.get("REPARSE_TEST_REDIS_URL") or os.environ.get("QUEUE_RECOVERY_REDIS_URL")
    if not url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Reparse cancellation gate requires dedicated Redis")
        pytest.skip("Use isolated integration Redis")
    url = urlunsplit(urlsplit(url)._replace(path="/14"))
    rid = str(uuid.uuid4())
    async with db_session_factory() as db:
        db.add(FileResource(id=rid, channel_id=sample_channel.id, guid=rid, title_raw="Synthetic cancellation",
                            torrent_url="magnet:?xt=urn:btih:synthetic"))
        await db.commit()
    producer, first, successor = (RedisQueue(redis_url=url) for _ in range(3))
    monkeypatch.setattr("app.services.task_queue.task_queue", producer)
    monkeypatch.setattr("app.job_handlers._refresh_runtime_config", AsyncMock())
    entered = asyncio.Event()

    async def block_metadata(*args, **kwargs):
        await require_execution_ownership()
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.services.fetch_service._process_resource_metadata", block_metadata)
    for worker in (first, successor):
        worker.register("reprocess_resource_metadata", _handle_reprocess_resource_metadata)
    await producer.start(consume=False)
    await first.start()
    try:
        assert (await client.post(f"/api/v1/resources/{rid}/reparse-metadata")).status_code == 200
        await asyncio.wait_for(entered.wait(), 10)
        before = await producer.status(f"reprocess-resource:{rid}")
        async with db_session_factory() as db:
            request = await db.scalar(select(ResourceReparseRequest).where(ResourceReparseRequest.resource_id == rid))
            request_id = request.id
        await first.stop()  # Actual queue cancellation and descriptor requeue, not SIGKILL.
        state = await producer.status(f"reprocess-resource:{rid}")
        assert state["status"] == "queued" and state["job_id"] == before["job_id"]
        async with db_session_factory() as db:
            assert await db.get(ResourceReparseRequest, request_id), "Cancelled work must remain durable"
        replay = AsyncMock()
        monkeypatch.setattr("app.services.fetch_service._process_resource_metadata", replay)
        await successor.start()
        async with asyncio.timeout(10):
            while (await producer.status(f"reprocess-resource:{rid}"))["status"] != "done":
                await asyncio.sleep(0.02)
        replay.assert_awaited_once()
        async with db_session_factory() as db:
            assert await db.get(ResourceReparseRequest, request_id) is None
        record_property("actual_worker_cancel_requeue", True)
        record_property("same_job_replayed", True)
    finally:
        await first.stop()
        await successor.stop()
        await producer._redis.delete(f"rssripple:job:reprocess-resource:{rid}",
                                     f"rssripple:active:reprocess-resource:{rid}",
                                     first._processing_key, successor._processing_key)
        await producer.stop()
