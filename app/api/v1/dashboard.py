"""Dashboard API routes."""

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.agent import Agent
from app.models.download_task import DownloadTask
from app.models.pending_decision import PendingDecision
from app.schemas.download_task import DownloadTaskResponse
from app.schemas.pending_decision import PendingDecisionResponse
from app.schemas.common import success_response

router = APIRouter()


@router.get("/dashboard")
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    # Active agents count
    active_q = await db.execute(
        select(func.count()).select_from(Agent).where(Agent.status == "active")
    )
    active_agents = active_q.scalar_one()

    # Active downloads (downloading or queued)
    active_dl_q = await db.execute(
        select(DownloadTask).where(
            DownloadTask.status.in_(["downloading", "queued", "pending"])
        ).order_by(DownloadTask.created_at.desc()).limit(20)
    )
    active_downloads = [
        DownloadTaskResponse.model_validate(t).model_dump()
        for t in active_dl_q.scalars().all()
    ]

    # Pending decisions
    pending_q = await db.execute(
        select(PendingDecision).where(PendingDecision.status == "pending")
        .order_by(PendingDecision.created_at.desc()).limit(10)
    )
    pending_decisions = [
        PendingDecisionResponse.model_validate(d).model_dump()
        for d in pending_q.scalars().all()
    ]

    return success_response({
        "active_agents": active_agents,
        "active_downloads": active_downloads,
        "pending_decisions": pending_decisions,
    })
