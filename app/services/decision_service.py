"""Shared state transitions for pending download decisions."""

from sqlalchemy import func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.pending_decision import PendingDecision
from app.services.resource_confirmation import LEGACY_CONFIRMATION_REASON_PREFIXES


async def maybe_reset_agent_run_status(db: AsyncSession, agent_id: str) -> None:
    """Mark an Agent run successful after its last choice todo is resolved."""
    agent = await db.get(Agent, agent_id)
    if not agent or agent.last_run_status != "pending_decisions":
        return
    choice_filter = not_(or_(*(
        PendingDecision.reason.startswith(prefix)
        for prefix in LEGACY_CONFIRMATION_REASON_PREFIXES
    )))
    remaining = (await db.execute(
        select(func.count()).select_from(PendingDecision).where(
            PendingDecision.agent_id == agent_id,
            PendingDecision.status == "pending",
            choice_filter,
        )
    )).scalar_one()
    if remaining == 0:
        agent.last_run_status = "success"
