"""Transactional request storage; queue delivery never owns acknowledgement."""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_resource_request import AgentResourceRequest
from app.utils.time import utcnow


@dataclass(frozen=True)
class RequestSnapshot:
    id: str
    revision: int
    resource_id: str


async def request_resources(db: AsyncSession, agent_ids: list[str], resource_ids: list[str]) -> None:
    """Write with the resource edit transaction; never commit or enqueue here."""
    table = AgentResourceRequest
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    now = utcnow()
    for agent_id in dict.fromkeys(agent_ids):
        for resource_id in dict.fromkeys(resource_ids):
            statement = (
                insert(table)
                .values(
                    id=str(uuid.uuid4()),
                    agent_id=agent_id,
                    resource_id=resource_id,
                    revision=1,
                    requested_at=now,
                    attempt_count=0,
                )
                .on_conflict_do_update(
                    index_elements=["agent_id", "resource_id"],
                    set_={
                        "revision": table.revision + 1,
                        "requested_at": now,
                        "attempt_count": 0,
                        "next_attempt_at": None,
                        "error_message": None,
                    },
                )
            )
            await db.execute(statement)


async def snapshot_requests(db: AsyncSession, agent_id: str) -> list[RequestSnapshot]:
    from app.models.agent import Agent
    from app.models.file_resource import FileResource

    table = AgentResourceRequest
    rows = (
        await db.execute(
            select(table.id, table.revision, table.resource_id)
            .join(Agent, Agent.id == table.agent_id)
            .join(FileResource, FileResource.id == table.resource_id)
            .where(
                table.agent_id == agent_id,
                Agent.channel_id == FileResource.channel_id,
                or_(table.next_attempt_at.is_(None), table.next_attempt_at <= utcnow()),
            )
        )
    ).all()
    return [RequestSnapshot(*row) for row in rows]


async def acknowledge_requests(db: AsyncSession, requests: list[RequestSnapshot]) -> None:
    """An older run must not delete a new edit or a recreated request row."""
    for request in requests:
        await db.execute(
            delete(AgentResourceRequest).where(
                AgentResourceRequest.id == request.id,
                AgentResourceRequest.revision == request.revision,
            )
        )


async def request_channel_resources(db: AsyncSession, channel_id: str, resource_ids: list[str]) -> list[str]:
    from app.models.agent import Agent

    agent_ids = list(
        (
            await db.scalars(
                select(Agent.id).where(
                    Agent.channel_id == channel_id,
                    Agent.status == "active",
                )
            )
        ).all()
    )
    await request_resources(db, agent_ids, resource_ids)
    return agent_ids


async def wake_agents(agent_ids: list[str], resource_ids: list[str] | None = None) -> None:
    import logging

    from app.services import task_queue

    for agent_id in agent_ids:
        try:
            payload = {"agent_id": agent_id, "resource_ids": resource_ids or [], "automatic": True}
            if resource_ids is None:
                payload["pending_requests"] = True
            await task_queue.task_queue.enqueue("run_agent", f"agent:{agent_id}", payload)
        except Exception:
            logging.getLogger(__name__).exception("Could not enqueue persisted requests for Agent %s", agent_id)


async def defer_requests(db: AsyncSession, requests: list[RequestSnapshot], message: str) -> None:
    from datetime import timedelta

    from sqlalchemy import update

    table = AgentResourceRequest
    for request in requests:
        identity = (table.id == request.id, table.revision == request.revision)
        attempts = await db.scalar(
            update(table)
            .where(*identity)
            .values(
                attempt_count=table.attempt_count + 1,
                error_message=message[:2048],
            )
            .returning(table.attempt_count)
        )
        if attempts is not None:
            delay = min(1800, 30 * 2 ** min(attempts - 1, 6))
            await db.execute(update(table).where(*identity).values(next_attempt_at=utcnow() + timedelta(seconds=delay)))


async def dispatch_pending_requests() -> None:
    from sqlalchemy import or_

    from app.database import committed_session
    from app.models.agent import Agent
    from app.models.file_resource import FileResource

    table = AgentResourceRequest
    async with committed_session() as db:
        valid = (
            select(Agent.id)
            .join(FileResource, FileResource.channel_id == Agent.channel_id)
            .where(
                Agent.id == table.agent_id,
                FileResource.id == table.resource_id,
            )
            .exists()
        )
        await db.execute(delete(table).where(~valid))
        agent_ids = list(
            (
                await db.scalars(
                    select(Agent.id)
                    .join(table, table.agent_id == Agent.id)
                    .where(
                        Agent.status == "active",
                        or_(table.next_attempt_at.is_(None), table.next_attempt_at <= utcnow()),
                    )
                    .distinct()
                )
            ).all()
        )
    await wake_agents(agent_ids)
