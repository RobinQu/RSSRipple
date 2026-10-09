"""Overlapping real API transactions and Turso unique-request enforcement."""

import asyncio
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import _is_retryable_lock_error
from app.models.resource_reparse_request import ResourceReparseRequest
from app.services import resource_reparse_requests as service
from tests.api.test_resources import _make_resource


async def test_two_actual_requests_keep_one_durable_intent(
    client, sample_channel, db_session_factory, monkeypatch, record_property,
):
    rid = await _make_resource(db_session_factory, sample_channel.id)
    monkeypatch.setattr("app.database.async_session_factory", db_session_factory)
    enqueue = AsyncMock(return_value={"job_id": "synthetic"})
    monkeypatch.setattr("app.services.task_queue.task_queue.enqueue", enqueue)
    inserted, conflict = asyncio.Event(), asyncio.Event()
    original = service.create_request
    conflicts = 0
    insert_attempts = 0
    original_scalar = AsyncSession.scalar

    async def observe_insert(session, statement, *args, **kwargs):
        nonlocal insert_attempts
        if getattr(statement, "is_insert", False) and statement.table.name == "resource_reparse_requests":
            insert_attempts += 1
            if insert_attempts > 1:
                conflict.set()
        return await original_scalar(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", observe_insert)

    async def hold_insert_until_conflict(db, resource_id, channel_id):
        nonlocal conflicts
        try:
            request = await original(db, resource_id, channel_id)
        except DatabaseError as exc:
            assert _is_retryable_lock_error(exc)
            conflicts += 1
            conflict.set()
            raise
        if request:
            inserted.set()
            await asyncio.wait_for(conflict.wait(), 5)
            await asyncio.sleep(0.05)
        return request

    monkeypatch.setattr(service, "create_request", hold_insert_until_conflict)
    first = asyncio.create_task(client.post(f"/api/v1/resources/{rid}/reparse-metadata"))
    second = None
    try:
        await asyncio.wait_for(inserted.wait(), 5)
        second = asyncio.create_task(client.post(f"/api/v1/resources/{rid}/reparse-metadata"))
        replies = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 10)
        # The retry middleware never replays POST: the conflicting second
        # request's write-write conflict reaches the client (a 500 in
        # production; the ASGI test transport re-raises it) instead of being
        # transparently retried into the 409 duplicate path. The durable
        # state is identical either way — one request, one enqueue.
        first_reply, second_reply = replies
        assert first_reply.status_code == 200
        assert isinstance(second_reply, DatabaseError)
        assert _is_retryable_lock_error(second_reply)
        async with db_session_factory() as db:
            count = await db.scalar(select(func.count()).select_from(ResourceReparseRequest))
            assert count == 1
        enqueue.assert_awaited_once()
        assert insert_attempts >= 2
        record_property("overlapping_insert_attempts", insert_attempts)
        record_property("actual_turso_write_conflicts", conflicts)
        record_property("responses", "200,<write-write conflict>")
        record_property("durable_requests", count)
    finally:
        conflict.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*[t for t in (first, second) if t], return_exceptions=True)
