"""Durable reparse requests; the queue only delivers wakeups after DB commit."""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import committed_session, retry_on_lock
from app.models.file_resource import FileResource
from app.models.resource_reparse_request import ResourceReparseRequest
from app.utils.time import utcnow

logger = logging.getLogger(__name__)
_RECHECK_SECONDS = 30
_BATCH_SIZE = 50


@dataclass(frozen=True)
class ReparseRequest:
    id: str
    resource_id: str
    channel_id: str


async def create_request(db: AsyncSession, resource_id: str, channel_id: str) -> ReparseRequest | None:
    """Join the caller's transaction. A duplicate never mutates the old request."""
    table = ResourceReparseRequest
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    request_id = str(uuid.uuid4())
    stored = await db.scalar(
        insert(table).values(
            id=request_id, resource_id=resource_id, requested_at=utcnow(),
            next_attempt_at=utcnow() + timedelta(seconds=_RECHECK_SECONDS), attempt_count=0,
        ).on_conflict_do_nothing(index_elements=["resource_id"]).returning(table.id)
    )
    return ReparseRequest(stored, resource_id, channel_id) if stored else None


async def finish_request(db: AsyncSession, request_id: str, resource_id: str) -> None:
    """A stale completion cannot acknowledge a recreated request for this resource."""
    await db.execute(delete(ResourceReparseRequest).where(
        ResourceReparseRequest.id == request_id, ResourceReparseRequest.resource_id == resource_id,
    ))


async def _record_delivery(request: ReparseRequest, failed: bool) -> None:
    async def record():
        table = ResourceReparseRequest
        async with committed_session() as db:
            values = {"next_attempt_at": utcnow() + timedelta(seconds=_RECHECK_SECONDS)}
            if failed:
                # Do not persist exception text: Redis URLs may contain credentials.
                values.update(error_message="Queue delivery unavailable", attempt_count=table.attempt_count + 1)
            else:
                values["error_message"] = None
            count = await db.scalar(
                update(table).where(table.id == request.id).values(**values).returning(table.attempt_count)
            )
            if failed and count is not None:
                delay = min(1800, _RECHECK_SECONDS * 2 ** min(count - 1, 6))
                await db.execute(update(table).where(table.id == request.id).values(
                    next_attempt_at=utcnow() + timedelta(seconds=delay),
                ))
    await retry_on_lock(record)


async def wake_request(request: ReparseRequest) -> bool:
    from app.services import task_queue

    try:
        await task_queue.task_queue.enqueue(
            "reprocess_resource_metadata", f"reprocess-resource:{request.resource_id}",
            {"request_id": request.id, "resource_id": request.resource_id, "channel_id": request.channel_id},
        )
    except Exception:
        await _record_delivery(request, failed=True)
        logger.warning("Queue delivery deferred for reparse request %s", request.id)
        return False
    # None means a live job has the key. Keep the durable request so a later
    # sweep can deliver it if that job belongs to an older request.
    await _record_delivery(request, failed=False)
    return True


async def dispatch_pending_reparses() -> None:
    async def claim():
        table = ResourceReparseRequest
        async with committed_session() as db:
            rows = (await db.execute(
                select(table.id, table.resource_id, FileResource.channel_id)
                .join(FileResource, FileResource.id == table.resource_id)
                .where(table.next_attempt_at <= utcnow())
                .order_by(table.next_attempt_at, table.id).limit(_BATCH_SIZE)
                .with_for_update(skip_locked=True, of=table)
            )).all()
            requests = [ReparseRequest(*row) for row in rows]
            if requests:
                await db.execute(update(table).where(table.id.in_([r.id for r in requests])).values(
                    next_attempt_at=utcnow() + timedelta(seconds=_RECHECK_SECONDS),
                ))
        return requests
    # Turso conflicts retry the DB-only claim; no transaction spans enqueue.
    for request in await retry_on_lock(claim):
        await wake_request(request)
