"""Prevent mixed timestamp/event consumption before workers or API writes start."""

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models.agent import Agent
from app.models.agent_publication_progress import AgentPublicationProgress
from app.models.app_setting import AppSetting
from app.models.file_resource import FileResource
from app.models.resource_publication import ResourcePublication
from app.services.publication_migration import MARKER


async def ensure_publication_ready(db):
    marker = await db.get(AppSetting, MARKER)
    if marker is None:
        legacy_resource = await db.scalar(select(FileResource.id).limit(1))
        legacy_agent = await db.scalar(select(Agent.id).where(Agent.last_consumed_at.is_not(None)).limit(1))
        if legacy_resource or legacy_agent:
            raise ValueError(
                "Publication migration required: stop writers and run scripts.review_publication_migration"
            )
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        await db.execute(
            insert(AppSetting).values(key=MARKER, value="fresh").on_conflict_do_nothing(index_elements=["key"])
        )
    unpublished = await db.scalar(
        select(FileResource.id)
        .where(
            ~exists(
                select(ResourcePublication.id).where(
                    ResourcePublication.resource_id == FileResource.id, ResourcePublication.kind == "created"
                )
            )
        )
        .limit(1)
    )
    if unpublished:
        raise ValueError("Resource without publication detected; stop legacy writers and review migration state")
    missing_progress = await db.scalar(
        select(Agent.id)
        .where(
            Agent.last_consumed_at.is_not(None),
            ~exists(
                select(AgentPublicationProgress.id).where(
                    AgentPublicationProgress.agent_id == Agent.id,
                    AgentPublicationProgress.channel_id == Agent.channel_id,
                )
            ),
        )
        .limit(1)
    )
    if missing_progress:
        raise ValueError("Agent publication progress missing or channel mismatch; review migration state")
