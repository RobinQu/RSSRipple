"""Reserve before RPC; persist its result once in the caller's transaction."""

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.models.download_dispatch import DownloadDispatch
from app.models.download_task import DownloadTask


@dataclass(frozen=True)
class DispatchReservation:
    operation_key: str
    task_id: str
    settled: bool


def operation_key(job_identity, resource_id, agent_id):
    body = ["download-v1", *job_identity, resource_id, agent_id]
    return hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()


def payload_digest(payload):
    # Distinguish a URL string from cached torrent bytes, even with equal bytes.
    raw = b"bytes:" + payload if isinstance(payload, bytes) else b"url:" + payload.encode()
    return hashlib.sha256(raw).hexdigest()


async def reserve_dispatch(bind, key, parameters, *, job_identity=None):
    """Own a short intent transaction; callers must not hold a write lock.

    This commits only an immutable reservation, never the caller's business
    writes. It does not hold a DB transaction across an external RPC.
    """
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    async with AsyncSession(bind=engine, expire_on_commit=False) as db, db.begin():
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        await db.execute(
            insert(DownloadDispatch).values(
                operation_key=key, task_id=str(uuid.uuid4()), parameters=parameters,
                job_key=job_identity[0] if job_identity else None,
                job_id=job_identity[1] if job_identity else None,
            ).on_conflict_do_nothing(index_elements=["operation_key"])
        )
        row = await db.scalar(select(DownloadDispatch).where(DownloadDispatch.operation_key == key))
        if row.parameters != parameters:
            raise ValueError("Queued download parameters changed; a new dispatch operation is required")
        return DispatchReservation(key, row.task_id, row.settled)


async def persist_dispatch_result(db, reservation, values):
    # Serialize result publication with the intent row, after all RPC work.
    # A previously settled but deleted task is a tombstone, not a retry slot.
    settled = await db.scalar(
        update(DownloadDispatch)
        .where(DownloadDispatch.operation_key == reservation.operation_key)
        .values(settled=DownloadDispatch.settled)
        .returning(DownloadDispatch.settled)
    )
    if settled is None:
        raise ValueError("Queued download reservation disappeared")
    if settled:
        existing = await db.get(DownloadTask, reservation.task_id, populate_existing=True)
        if existing is None:
            raise ValueError("Queued download task was removed; refusing to recreate it")
        return existing
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(DownloadTask).values(id=reservation.task_id, **values)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await db.execute(
        update(DownloadDispatch)
        .where(DownloadDispatch.operation_key == reservation.operation_key)
        .values(settled=True)
    )
    task = await db.get(DownloadTask, reservation.task_id, populate_existing=True)
    if task is None:
        raise ValueError("Reserved download task disappeared during persistence")
    return task
