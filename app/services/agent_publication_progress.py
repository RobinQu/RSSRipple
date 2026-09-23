"""Prototype event snapshots: caller owns transaction and processing success."""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models.agent_publication_progress import AgentPublicationProgress
from app.models.file_resource import FileResource
from app.models.resource_publication import ChannelPublicationCounter, ResourcePublication


@dataclass(frozen=True)
class PublicationSnapshot:
    agent_id: str
    channel_id: str
    generation: str
    through: int
    resource_ids: tuple[str, ...]


async def published_prefix(db, channel_id):
    return (
        await db.scalar(
            select(ChannelPublicationCounter.sequence).where(ChannelPublicationCounter.channel_id == channel_id)
        )
    ) or 0


async def reset_progress(db, agent_id, channel_id):
    """Exclude currently published history; future commits stay eligible.

    Call in the successful backfill save transaction, after all network work.
    This is also the explicit first-run baseline; legacy migration is separate.
    """
    prefix = await published_prefix(db, channel_id)
    values = dict(
        channel_id=channel_id,
        generation=str(uuid.uuid4()),
        baseline=prefix,
        cursor=prefix,
        historical_created_after=None,
    )
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(AgentPublicationProgress)
        .values(id=str(uuid.uuid4()), agent_id=agent_id, **values)
        .on_conflict_do_update(index_elements=["agent_id"], set_=values)
    )


async def snapshot_publications(db, agent_id, channel_id):
    # Select scalar columns to avoid stale identity-map state after a reset.
    row = (
        await db.execute(
            select(
                AgentPublicationProgress.generation,
                AgentPublicationProgress.baseline,
                AgentPublicationProgress.cursor,
                AgentPublicationProgress.historical_created_after,
            ).where(
                AgentPublicationProgress.agent_id == agent_id,
                AgentPublicationProgress.channel_id == channel_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise ValueError("Agent publication baseline must be initialized explicitly")
    prefix = await published_prefix(db, channel_id)
    admission = ResourcePublication.origin_sequence > row.baseline
    if row.historical_created_after is not None:
        admission = or_(admission, FileResource.created_at > row.historical_created_after)
    ids = await db.scalars(
        select(ResourcePublication.resource_id)
        .join(FileResource, FileResource.id == ResourcePublication.resource_id)
        .where(
            ResourcePublication.channel_id == channel_id,
            ResourcePublication.sequence > row.cursor,
            ResourcePublication.sequence <= prefix,
            admission,
        )
        .order_by(ResourcePublication.sequence)
    )
    return PublicationSnapshot(agent_id, channel_id, row.generation, prefix, tuple(dict.fromkeys(ids)))


async def acknowledge_publications(db, snapshot):
    """Acknowledge only after successful processing; never overwrite a reset."""
    result = await db.execute(
        update(AgentPublicationProgress)
        .where(
            AgentPublicationProgress.agent_id == snapshot.agent_id,
            AgentPublicationProgress.channel_id == snapshot.channel_id,
            AgentPublicationProgress.generation == snapshot.generation,
            AgentPublicationProgress.cursor < snapshot.through,
        )
        .values(cursor=snapshot.through)
    )
    return result.rowcount == 1


async def initialize_first_run(db, agent):
    """Initialize only a never-consumed Agent, without resetting existing state.

    Historical time watermarks require the separate reviewed migration; a
    channel mismatch requires an explicit channel-change baseline policy.
    """
    existing = await db.scalar(
        select(AgentPublicationProgress.channel_id).where(AgentPublicationProgress.agent_id == agent.id)
    )
    if existing is not None:
        if existing != agent.channel_id:
            raise ValueError("Agent publication channel changed without a baseline reset")
        return False
    if agent.last_consumed_at is not None:
        raise ValueError("Legacy Agent requires publication progress migration")
    prefix = await published_prefix(db, agent.channel_id)
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    result = await db.execute(
        insert(AgentPublicationProgress)
        .values(
            id=str(uuid.uuid4()),
            agent_id=agent.id,
            channel_id=agent.channel_id,
            generation=str(uuid.uuid4()),
            baseline=prefix,
            cursor=prefix,
        )
        .on_conflict_do_nothing(index_elements=["agent_id"])
    )
    return result.rowcount == 1


async def switch_channel_progress(db, agent):
    """Null backfill saves retain the previous timestamp's historical scope.

    New publications after this save are always eligible, even if their
    created_at predates that timestamp. Explicit backfill uses reset_progress.
    """
    if agent.last_consumed_at is None:
        await db.execute(delete(AgentPublicationProgress).where(AgentPublicationProgress.agent_id == agent.id))
        return
    prefix = await published_prefix(db, agent.channel_id)
    values = dict(
        channel_id=agent.channel_id,
        generation=str(uuid.uuid4()),
        baseline=prefix,
        cursor=0,
        historical_created_after=agent.last_consumed_at,
    )
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(AgentPublicationProgress)
        .values(id=str(uuid.uuid4()), agent_id=agent.id, **values)
        .on_conflict_do_update(index_elements=["agent_id"], set_=values)
    )


async def acknowledge_window(db, snapshot, scan_since):
    """Keep later completion events for a successfully scanned historical range.

    Admit that range without widening beyond the explicit scan. Never consume
    events published after the snapshot or overwrite a later backfill reset.
    """
    from datetime import datetime

    from sqlalchemy import case

    cutoff = scan_since if scan_since is not None else datetime.min
    table = AgentPublicationProgress
    result = await db.execute(
        update(table)
        .where(
            table.agent_id == snapshot.agent_id,
            table.channel_id == snapshot.channel_id,
            table.generation == snapshot.generation,
        )
        .values(
            cursor=case((table.cursor < snapshot.through, snapshot.through), else_=table.cursor),
            historical_created_after=case(
                (or_(table.historical_created_after.is_(None), table.historical_created_after > cutoff), cutoff),
                else_=table.historical_created_after,
            ),
        )
    )
    return result.rowcount == 1


async def prepare_window_retry(db, snapshot, resource_ids, scan_since):
    """Persist explicit scan intent before processing can commit or crash.

    Rewind only as far as the earliest selected creation event. Task/decision
    dedup makes already-consumed eligible neighbours safe to reconsider.
    A new generation invalidates runs that selected an older cursor state.
    """
    from dataclasses import replace
    from datetime import datetime

    from sqlalchemy import case, func

    earliest = await db.scalar(
        select(func.min(ResourcePublication.sequence)).where(
            ResourcePublication.channel_id == snapshot.channel_id,
            ResourcePublication.kind == "created",
            ResourcePublication.resource_id.in_(resource_ids),
        )
    )
    if earliest is None:
        raise ValueError("Selected scan resources require creation publications")
    cutoff = scan_since if scan_since is not None else datetime.min
    generation = str(uuid.uuid4())
    table = AgentPublicationProgress
    result = await db.execute(
        update(table)
        .where(
            table.agent_id == snapshot.agent_id,
            table.channel_id == snapshot.channel_id,
            table.generation == snapshot.generation,
        )
        .values(
            generation=generation,
            cursor=case((table.cursor >= earliest, earliest - 1), else_=table.cursor),
            historical_created_after=case(
                (or_(table.historical_created_after.is_(None), table.historical_created_after > cutoff), cutoff),
                else_=table.historical_created_after,
            ),
        )
    )
    if result.rowcount != 1:
        raise ValueError("Agent consumption scope changed; retry scan with current rules")
    return replace(snapshot, generation=generation)


async def require_current_scope(db, snapshot):
    """Check a fresh committed view, not a long-lived Turso read snapshot.

    This fences work before dispatch begins. It cannot revoke an RPC that
    already began before a concurrent edit committed.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent

    async with AsyncSession(bind=db.bind) as current:
        valid = await current.scalar(
            select(AgentPublicationProgress.id)
            .join(Agent, Agent.id == AgentPublicationProgress.agent_id)
            .where(
                Agent.id == snapshot.agent_id,
                Agent.channel_id == snapshot.channel_id,
                AgentPublicationProgress.channel_id == snapshot.channel_id,
                AgentPublicationProgress.generation == snapshot.generation,
            )
        )
    if valid is None:
        raise ValueError("Agent consumption scope changed; old run must not dispatch")


async def invalidate_running_scope(db, agent_id):
    """Rule edits invalidate dispatch snapshots without moving consumption."""
    await db.execute(
        update(AgentPublicationProgress)
        .where(AgentPublicationProgress.agent_id == agent_id)
        .values(generation=str(uuid.uuid4()))
    )
