"""Dashboard API routes."""

import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clients.downloader import get_downloader_client
from app.config import settings
from app.database import get_db
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.organize_plan import OrganizePlan
from app.models.pending_decision import PendingDecision
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

    # Untracked torrents: actively downloading in a downloader but without a
    # matching non-terminal DownloadTask (e.g. added directly in Transmission).
    # Lets users see downloads RSSRipple did not dispatch.
    untracked_tasks: list[dict] = []
    tracked_q = await db.execute(
        select(DownloadTask.downloader_id, DownloadTask.transmission_torrent_id).where(
            DownloadTask.status.in_(["pending", "queued", "downloading", "paused"]),
            DownloadTask.transmission_torrent_id.isnot(None),
        )
    )
    tracked: dict[str, set[int]] = {}
    for dl_id, tid in tracked_q.all():
        tracked.setdefault(dl_id, set()).add(tid)

    downloaders = (await db.execute(select(DownloaderInstance))).scalars().all()

    async def _list_torrents(downloader: DownloaderInstance) -> list[dict]:
        try:
            wrapper = get_downloader_client(downloader)
            return await asyncio.wait_for(
                wrapper.list_torrents(), timeout=settings.transmission_timeout
            )
        except Exception:
            # An unreachable downloader must not break the dashboard.
            return []

    torrent_lists = await asyncio.gather(*(_list_torrents(d) for d in downloaders))
    for downloader, torrents in zip(downloaders, torrent_lists, strict=True):
        tracked_ids = tracked.get(downloader.id, set())
        for torrent in torrents:
            if (
                torrent.get("status") in ("downloading", "download pending")
                and not torrent.get("is_finished")
                and torrent.get("id") not in tracked_ids
            ):
                untracked_tasks.append({
                    # Synthetic id — there is no DownloadTask row for these.
                    "task_id": f"untracked-{downloader.id}-{torrent['id']}",
                    "resource_title": torrent.get("name", ""),
                    "progress": torrent.get("percent_done", 0.0),
                    "agent_id": None,
                    "agent_name": None,
                    "channel_id": None,
                    "channel_name": None,
                    "downloader_id": downloader.id,
                    "downloader_name": downloader.name,
                })

    if untracked_tasks:
        active_download_groups.append({
            "type": "untracked",
            "id": None,
            "title": "未跟踪",
            "poster_url": None,
            "tasks": untracked_tasks,
        })

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
    from app.schemas.file_resource import FileResourceResponse

    pending_decisions = []
    for pd in pd_q.scalars().all():
        # Load candidate resources (full rows — the frontend renders raw titles
        # and, for ambiguous episode/season decisions, an inline correction
        # form keyed off ``episode_confidence``).
        res_q = await db.execute(
            select(FileResource)
            .where(FileResource.id.in_(pd.candidates or []))
            .options(
                selectinload(FileResource.audio_work),
                selectinload(FileResource.collection),
            )
        )
        candidates = res_q.scalars().all()
        pending_decisions.append({
            "id": pd.id,
            "agent_id": pd.agent_id,
            "agent_name": pd.agent.name if pd.agent else None,
            "series_id": pd.series_id,
            "movie_id": pd.movie_id,
            "episode": pd.episode,
            "season": pd.season,
            "reason": pd.reason,
            "llm_suggestion": pd.llm_suggestion,
            "candidates": [c.id for c in candidates],
            "candidate_resources": [
                FileResourceResponse.model_validate(c).model_dump()
                for c in candidates
            ],
            "title": (
                (pd.series.title_cn or pd.series.title_en) if pd.series_id and pd.series
                else (pd.movie.title_cn or pd.movie.title_en) if pd.movie_id and pd.movie
                else "Unknown"
            ),
            "created_at": pd.created_at.isoformat(),
        })

    # Pending organize plans (top 10) — they are actionable todos too, surfaced
    # on the dashboard with the same in-place execute/cancel/detail operations.
    # Reuse the plan list-item builder from the organize routes (includes the
    # ops preview so the frontend can render file paths inline).
    from app.api.v1.organize import _PLAN_LOAD_OPTIONS, _plan_list_item

    pending_plans = []
    plan_q = await db.execute(
        select(OrganizePlan)
        .where(OrganizePlan.status == "pending")
        .order_by(OrganizePlan.created_at.desc())
        .limit(10)
        .options(*_PLAN_LOAD_OPTIONS)
    )
    for p in plan_q.scalars().all():
        pending_plans.append(_plan_list_item(p))

    return success_response({
        "active_agents": active_agents,
        "active_channels": active_channels,
        "active_download_count": len(tasks) + len(untracked_tasks),
        "active_download_groups": active_download_groups,
        "pending_decisions": pending_decisions,
        "pending_plans": pending_plans,
    })
