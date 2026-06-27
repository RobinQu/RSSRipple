"""Dashboard API routes."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.series import TVSeries
from app.schemas.common import success_response

router = APIRouter()


@router.get("/dashboard")
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    active_agents_q = await db.execute(
        select(func.count()).select_from(Agent).where(Agent.status == "active")
    )
    active_agents = active_agents_q.scalar_one() or 0

    active_channels_q = await db.execute(
        select(func.count()).select_from(Channel).where(Channel.status == "active")
    )
    active_channels = active_channels_q.scalar_one() or 0

    # Active download tasks
    active_statuses = ["pending", "queued", "downloading"]
    tasks_q = await db.execute(
        select(DownloadTask)
        .where(DownloadTask.status.in_(active_statuses))
        .options(
            selectinload(DownloadTask.agent).selectinload(Agent.channel),
            selectinload(DownloadTask.file_resource).selectinload(FileResource.series),
            selectinload(DownloadTask.file_resource).selectinload(FileResource.movie),
        )
    )
    tasks = tasks_q.scalars().all()

    groups: dict[tuple, dict] = {}
    unknown_key = ("unknown", None)
    groups[unknown_key] = {
        "type": "unknown", "id": None, "title": "未识别", "poster_url": None, "tasks": [],
    }

    for task in tasks:
        resource = task.file_resource
        agent = task.agent
        task_entry = {
            "task_id": task.id,
            "resource_title": resource.title_raw if resource else "",
            "progress": task.progress,
            "agent_id": agent.id if agent else None,
            "agent_name": agent.name if agent else None,
            "channel_id": agent.channel_id if agent else None,
            "channel_name": agent.channel.name if agent and agent.channel else None,
        }
        if resource and resource.series_id and resource.series:
            key = ("series", resource.series_id)
            if key not in groups:
                s = resource.series
                groups[key] = {
                    "type": "series", "id": resource.series_id,
                    "title": s.title_cn or s.title_en or s.original_title or "Unknown",
                    "poster_url": s.poster_url,
                    "tasks": [],
                }
            groups[key]["tasks"].append(task_entry)
        elif resource and resource.movie_id and resource.movie:
            key = ("movie", resource.movie_id)
            if key not in groups:
                m = resource.movie
                groups[key] = {
                    "type": "movie", "id": resource.movie_id,
                    "title": m.title_cn or m.title_en or m.original_title or "Unknown",
                    "poster_url": m.poster_url,
                    "tasks": [],
                }
            groups[key]["tasks"].append(task_entry)
        else:
            groups[unknown_key]["tasks"].append(task_entry)

    # Only include unknown if it has tasks
    active_download_groups = [g for g in groups.values() if g["tasks"] or g["type"] != "unknown"]

    # Pending decisions (top 10)
    pd_q = await db.execute(
        select(PendingDecision)
        .where(PendingDecision.status == "pending")
        .order_by(PendingDecision.created_at.desc())
        .limit(10)
        .options(
            selectinload(PendingDecision.series),
            selectinload(PendingDecision.movie),
            selectinload(PendingDecision.agent),
        )
    )
    pending_decisions = []
    for pd in pd_q.scalars().all():
        # Load candidate resources
        res_q = await db.execute(
            select(FileResource).where(FileResource.id.in_(pd.candidates or []))
        )
        candidates = res_q.scalars().all()
        pending_decisions.append({
            "id": pd.id,
            "agent_id": pd.agent_id,
            "agent_name": pd.agent.name if pd.agent else None,
            "series_id": pd.series_id,
            "movie_id": pd.movie_id,
            "episode": pd.episode,
            "reason": pd.reason,
            "llm_suggestion": pd.llm_suggestion,
            "candidates": [
                {
                    "id": c.id,
                    "title_raw": c.title_raw,
                    "subtitle_group": c.subtitle_group,
                    "resolution": c.resolution,
                    "source": c.source,
                    "file_size": c.file_size,
                    "published_at": c.published_at.isoformat() if c.published_at else None,
                }
                for c in candidates
            ],
            "title": (
                (pd.series.title_cn or pd.series.title_en) if pd.series_id and pd.series
                else (pd.movie.title_cn or pd.movie.title_en) if pd.movie_id and pd.movie
                else "Unknown"
            ),
            "created_at": pd.created_at.isoformat(),
        })

    return success_response({
        "active_agents": active_agents,
        "active_channels": active_channels,
        "active_download_count": len(tasks),
        "active_download_groups": active_download_groups,
        "pending_decisions": pending_decisions,
    })
