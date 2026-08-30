"""Dashboard API routes."""

import asyncio

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, not_, or_, select
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
from app.models.movie import Movie
from app.models.organize_audit import OrganizeAuditEntry
from app.models.organize_plan import OrganizePlan
from app.models.pending_decision import PendingDecision
from app.models.series import TVSeries
from app.schemas.common import success_response
from app.schemas.dashboard import DashboardTodoIgnoreRequest
from app.schemas.file_resource import FileResourceResponse
from app.services.decision_service import maybe_reset_agent_run_status
from app.services.resource_confirmation import (
    LEGACY_CONFIRMATION_REASON_PREFIXES,
    ResourceConfirmation,
    inspect_resource_confirmation,
)
from app.utils.time import utcnow

router = APIRouter()

_CONFIRMATION_SCAN_BATCH_SIZE = 200


def _serialize_confirmation(
    resource: FileResource, confirmation: ResourceConfirmation,
) -> dict:
    return {
        "resource": FileResourceResponse.model_validate(resource).model_dump(),
        "channel_name": resource.channel.name if resource.channel else None,
        "work_title": (
            (resource.series.title_cn or resource.series.title_en)
            if resource.series_id and resource.series
            else (resource.movie.title_cn or resource.movie.title_en)
            if resource.movie_id and resource.movie
            else None
        ),
        "kinds": list(confirmation.kinds),
        "missing_fields": list(confirmation.missing_fields),
    }


async def _page_pending_decisions(
    db: AsyncSession, page: int, page_size: int,
) -> tuple[list[dict], int]:
    filters = (
        PendingDecision.status == "pending",
        not_(
            or_(
                *(
                    PendingDecision.reason.startswith(prefix)
                    for prefix in LEGACY_CONFIRMATION_REASON_PREFIXES
                )
            )
        ),
    )
    # Candidate cardinality is stored in a JSON column whose length function
    # differs between PostgreSQL and Turso. Select the small decision summary
    # set once and apply the canonical >=2 gate in Python for dialect parity.
    summaries = (await db.execute(
        select(PendingDecision.id, PendingDecision.candidates)
        .where(*filters)
        .order_by(PendingDecision.created_at.desc(), PendingDecision.id.desc())
    )).all()
    eligible_ids = [row.id for row in summaries if len(row.candidates or []) >= 2]
    total = len(eligible_ids)
    page_ids = eligible_ids[(page - 1) * page_size:page * page_size]
    if not page_ids:
        return [], total

    decisions = (await db.execute(
        select(PendingDecision)
        .where(PendingDecision.id.in_(page_ids))
        .options(
            selectinload(PendingDecision.series),
            selectinload(PendingDecision.movie),
            selectinload(PendingDecision.agent),
        )
    )).scalars().all()
    decisions_by_id = {decision.id: decision for decision in decisions}
    ordered_decisions = [
        decisions_by_id[decision_id]
        for decision_id in page_ids
        if decision_id in decisions_by_id
    ]

    candidate_ids = {
        resource_id
        for decision in ordered_decisions
        for resource_id in (decision.candidates or [])
    }
    candidate_rows = (await db.execute(
        select(FileResource)
        .where(FileResource.id.in_(candidate_ids))
        .options(
            selectinload(FileResource.series),
            selectinload(FileResource.movie),
            selectinload(FileResource.audio_work),
            selectinload(FileResource.collection),
        )
    )).scalars().all()
    candidates_by_id = {resource.id: resource for resource in candidate_rows}

    items: list[dict] = []
    for decision in ordered_decisions:
        candidates = [
            candidates_by_id[resource_id]
            for resource_id in (decision.candidates or [])
            if resource_id in candidates_by_id
        ]
        items.append({
            "id": decision.id,
            "agent_id": decision.agent_id,
            "agent_name": decision.agent.name if decision.agent else None,
            "series_id": decision.series_id,
            "movie_id": decision.movie_id,
            "episode": decision.episode,
            "season": decision.season,
            "reason": decision.reason,
            "llm_suggestion": decision.llm_suggestion,
            "candidates": [candidate.id for candidate in candidates],
            "candidate_resources": [
                FileResourceResponse.model_validate(candidate).model_dump()
                for candidate in candidates
            ],
            "title": (
                (decision.series.title_cn or decision.series.title_en)
                if decision.series_id and decision.series
                else (decision.movie.title_cn or decision.movie.title_en)
                if decision.movie_id and decision.movie
                else "Unknown"
            ),
            "created_at": decision.created_at.isoformat(),
        })
    return items, total


async def _page_pending_confirmations(
    db: AsyncSession, page: int, page_size: int,
) -> tuple[list[dict], int]:
    """Page policy-derived confirmations while returning an exact total."""
    load_options = (
        selectinload(FileResource.channel),
        selectinload(FileResource.series).selectinload(TVSeries.collection),
        selectinload(FileResource.movie).selectinload(Movie.collection),
        selectinload(FileResource.audio_work),
        selectinload(FileResource.collection),
    )
    page_start = (page - 1) * page_size
    page_end = page_start + page_size
    matched = 0
    items: list[dict] = []
    # The confirmation policy spans resource, channel and work fields, so it
    # intentionally stays in the shared policy helper instead of duplicating
    # a fragile dialect-specific SQL predicate. Read only the ordered ids up
    # front, then hydrate resources in bounded batches; this avoids OFFSET's
    # quadratic full-table scan while keeping ORM relationship memory bounded.
    ordered_ids = (await db.execute(
        select(FileResource.id)
        .where(FileResource.confirmation_ignored_at.is_(None))
        .order_by(FileResource.created_at.desc(), FileResource.id.desc())
    )).scalars().all()
    for batch_start in range(0, len(ordered_ids), _CONFIRMATION_SCAN_BATCH_SIZE):
        batch_ids = ordered_ids[
            batch_start:batch_start + _CONFIRMATION_SCAN_BATCH_SIZE
        ]
        batch_rows = (await db.execute(
            select(FileResource)
            .where(FileResource.id.in_(batch_ids))
            .options(*load_options)
        )).scalars().all()
        rows_by_id = {resource.id: resource for resource in batch_rows}
        for resource_id in batch_ids:
            resource = rows_by_id.get(resource_id)
            if resource is None:
                continue  # deleted concurrently after the id snapshot
            required_fields = (
                resource.channel.required_metadata_fields
                if resource.channel else None
            )
            confirmation = inspect_resource_confirmation(resource, required_fields)
            if not confirmation.required:
                continue
            if page_start <= matched < page_end:
                items.append(_serialize_confirmation(resource, confirmation))
            matched += 1
    return items, matched


async def _page_pending_plans(
    db: AsyncSession, page: int, page_size: int,
) -> tuple[list[dict], int]:
    from app.api.v1.organize import _PLAN_LOAD_OPTIONS, _plan_list_item

    total = int((await db.execute(
        select(func.count()).select_from(OrganizePlan).where(
            OrganizePlan.status == "pending"
        )
    )).scalar_one() or 0)
    plans = (await db.execute(
        select(OrganizePlan)
        .where(OrganizePlan.status == "pending")
        .order_by(OrganizePlan.created_at.desc(), OrganizePlan.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .options(*_PLAN_LOAD_OPTIONS)
    )).scalars().all()
    return [_plan_list_item(plan) for plan in plans], total


@router.post("/dashboard/todos/ignore")
async def ignore_dashboard_todos(
    body: DashboardTodoIgnoreRequest,
    db: AsyncSession = Depends(get_db),
):
    """Persistently dismiss one or many dashboard todos of the same kind."""
    ids = list(dict.fromkeys(body.ids))
    ignored = 0
    affected_agents: set[str] = set()
    if body.kind == "decision":
        rows = (await db.execute(
            select(PendingDecision).where(
                PendingDecision.id.in_(ids),
                PendingDecision.status == "pending",
            )
        )).scalars().all()
        for row in rows:
            if (
                len(row.candidates or []) < 2
                or (row.reason or "").startswith(LEGACY_CONFIRMATION_REASON_PREFIXES)
            ):
                continue
            row.status = "skipped"
            row.decided_at = utcnow()
            affected_agents.add(row.agent_id)
            ignored += 1
        if affected_agents:
            await db.flush()
            for agent_id in affected_agents:
                await maybe_reset_agent_run_status(db, agent_id)
    elif body.kind == "confirmation":
        rows = (await db.execute(
            select(FileResource).where(FileResource.id.in_(ids))
        )).scalars().all()
        now = utcnow()
        for row in rows:
            if row.confirmation_ignored_at is None:
                row.confirmation_ignored_at = now
                ignored += 1
    else:
        rows = (await db.execute(
            select(OrganizePlan).where(
                OrganizePlan.id.in_(ids),
                OrganizePlan.status.in_(("pending", "failed")),
            )
        )).scalars().all()
        for row in rows:
            from_status = row.status
            row.status = "cancelled"
            db.add(OrganizeAuditEntry(
                plan_id=row.id,
                action="cancelled",
                detail={"from_status": from_status, "source": "dashboard_ignore"},
            ))
            ignored += 1
    await db.commit()
    return success_response({
        "requested": len(ids),
        "ignored": ignored,
        "unchanged": len(ids) - ignored,
    })


@router.get("/dashboard")
async def get_dashboard(
    decision_page: int = Query(1, ge=1),
    confirmation_page: int = Query(1, ge=1),
    plan_page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
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

    # Each todo tab is independently paged. Totals always describe the full
    # actionable set, so the headline metric and tab counts no longer stop at
    # the current page size.
    pending_decisions, pending_decisions_total = await _page_pending_decisions(
        db, decision_page, page_size
    )
    pending_confirmations, pending_confirmations_total = (
        await _page_pending_confirmations(db, confirmation_page, page_size)
    )
    pending_plans, pending_plans_total = await _page_pending_plans(
        db, plan_page, page_size
    )

    return success_response({
        "active_agents": active_agents,
        "active_channels": active_channels,
        "active_download_count": len(tasks) + len(untracked_tasks),
        "active_download_groups": active_download_groups,
        "pending_decisions": pending_decisions,
        "pending_decisions_total": pending_decisions_total,
        "pending_confirmations": pending_confirmations,
        "pending_confirmations_total": pending_confirmations_total,
        "pending_plans": pending_plans,
        "pending_plans_total": pending_plans_total,
    })
