"""Offline bootstrap of event order; caller holds the maintenance write barrier.

No commit or network calls. Run once before new publishers start. Existing
watermarks remain intact; ambiguous historical omissions are not replayed.
"""

import uuid
from bisect import bisect_right

from sqlalchemy import select, text

from app.models.agent import Agent
from app.models.agent_publication_progress import AgentPublicationProgress
from app.models.app_setting import AppSetting
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.resource_publication import ChannelPublicationCounter, ResourcePublication

MARKER = "resource-publication-bootstrap-v1"


async def bootstrap_publications(db, *, writers_stopped):
    if not writers_stopped:
        raise ValueError("Publication bootstrap requires stopped writers")
    await lock_publication_migration(db)
    # SQLite/Turso caller must start BEGIN IMMEDIATE before calling this helper.
    if await db.get(AppSetting, MARKER) is not None:
        return {"status": "already_applied"}
    for model in (ChannelPublicationCounter, ResourcePublication, AgentPublicationProgress):
        if await db.scalar(select(model.id).limit(1)) is not None:
            raise ValueError("Unmarked publication state exists; review before migration")
    migrated_resources = migrated_agents = 0
    channel_ids = list(await db.scalars(select(Channel.id).order_by(Channel.id)))
    for channel_id in channel_ids:
        resources = (
            await db.execute(
                select(FileResource.id, FileResource.created_at)
                .where(FileResource.channel_id == channel_id)
                .order_by(FileResource.created_at, FileResource.id)
            )
        ).all()
        timestamps = [row.created_at for row in resources]
        db.add(ChannelPublicationCounter(channel_id=channel_id, sequence=len(resources)))
        for sequence, row in enumerate(resources, 1):
            db.add(
                ResourcePublication(
                    channel_id=channel_id,
                    resource_id=row.id,
                    sequence=sequence,
                    origin_sequence=sequence,
                    kind="created",
                )
            )
        agents = (
            await db.execute(select(Agent.id, Agent.last_consumed_at).where(Agent.channel_id == channel_id))
        ).all()
        for agent in agents:
            if agent.last_consumed_at is None:
                # Preserve first-run semantics: initialize when first run occurs.
                continue
            baseline = bisect_right(timestamps, agent.last_consumed_at)
            db.add(
                AgentPublicationProgress(
                    agent_id=agent.id,
                    channel_id=channel_id,
                    generation=str(uuid.uuid4()),
                    baseline=baseline,
                    cursor=baseline,
                )
            )
            migrated_agents += 1
        migrated_resources += len(resources)
        await db.flush()
    db.add(AppSetting(key=MARKER, value="complete"))
    await db.flush()
    return {"status": "applied", "resources": migrated_resources, "agents": migrated_agents}


async def lock_publication_migration(db):
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text(
                "LOCK TABLE channels, agents, file_resources, channel_publication_counters, "
                "resource_publications, agent_publication_progress, app_settings IN SHARE ROW EXCLUSIVE MODE"
            )
        )
