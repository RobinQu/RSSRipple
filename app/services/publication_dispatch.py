"""Queue is a wakeup hint; durable publication progress owns pending work."""

import logging

from sqlalchemy import and_, or_, select

from app.database import committed_session
from app.models.agent import Agent
from app.models.agent_publication_progress import AgentPublicationProgress
from app.models.resource_publication import ChannelPublicationCounter

logger = logging.getLogger(__name__)


async def dispatch_pending_publications():
    from app.services import task_queue

    progress = AgentPublicationProgress
    async with committed_session() as db:
        agent_ids = list(
            await db.scalars(
                select(Agent.id)
                .outerjoin(progress, progress.agent_id == Agent.id)
                .outerjoin(ChannelPublicationCounter, ChannelPublicationCounter.channel_id == Agent.channel_id)
                .where(
                    Agent.status == "active",
                    or_(
                        and_(progress.id.is_(None), Agent.last_consumed_at.is_(None)),
                        and_(
                            progress.channel_id == Agent.channel_id,
                            ChannelPublicationCounter.sequence > progress.cursor,
                        ),
                    ),
                )
                .order_by(Agent.id)
            )
        )
    # No DB transaction spans a queue call. A failed/busy enqueue leaves the
    # same rows pending, and another tick (or worker) can deliver the wakeup.
    for agent_id in agent_ids:
        try:
            await task_queue.task_queue.enqueue(
                "run_agent", f"agent:{agent_id}", {"agent_id": agent_id, "automatic": True}
            )
        except Exception:
            logger.exception("Could not enqueue published resources for Agent %s", agent_id)
