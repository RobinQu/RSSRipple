"""TVSeries API routes."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.series import TVSeries
from app.schemas.common import paginated_response, success_response
from app.schemas.series import TVSeriesCreate, TVSeriesResponse, TVSeriesUpdate
from app.services import fts as fts_service

router = APIRouter()


@router.get("/series")
async def list_series(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="Title fuzzy search"),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    if search:
        # Use FTS5 for CJK-aware full-text search
        candidate_ids = await fts_service.search_series_fts(db, search, limit=200)
        if candidate_ids:
            base_q = select(TVSeries).where(TVSeries.id.in_(candidate_ids))
            total = len(candidate_ids)
            result = await db.execute(
                base_q.order_by(TVSeries.created_at.desc()).offset(offset).limit(page_size)
            )
        else:
            # Fallback to ILIKE if FTS5 returns nothing
            pattern = f"%{search}%"
            base_q = select(TVSeries).where(
                or_(
                    TVSeries.title_cn.ilike(pattern),
                    TVSeries.title_en.ilike(pattern),
                    TVSeries.original_title.ilike(pattern),
                )
            )
            total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
            total = total_q.scalar_one()
            result = await db.execute(
                base_q.order_by(TVSeries.created_at.desc()).offset(offset).limit(page_size)
            )
    else:
        base_q = select(TVSeries)
        total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
        total = total_q.scalar_one()
        result = await db.execute(
            base_q.order_by(TVSeries.created_at.desc()).offset(offset).limit(page_size)
        )
    items = result.scalars().all()
    return paginated_response(
        [TVSeriesResponse.model_validate(s).model_dump() for s in items],
        total=total, page=page, page_size=page_size,
    )


@router.post("/series", status_code=201)
async def create_series(
    body: TVSeriesCreate,
    db: AsyncSession = Depends(get_db),
):
    series = TVSeries(**body.model_dump())
    db.add(series)
    await db.flush()
    await fts_service.upsert_series_fts(db, series)
    await db.refresh(series)
    return success_response(TVSeriesResponse.model_validate(series).model_dump())


@router.get("/series/{series_id}")
async def get_series(series_id: str, db: AsyncSession = Depends(get_db)):
    from app.models.agent_work import AgentWork
    from app.models.download_task import DownloadTask
    from app.models.file_resource import FileResource
    from app.schemas.episode import EpisodeResponse
    from app.schemas.file_resource import FileResourceResponse

    series = await db.get(
        TVSeries, series_id,
        options=[selectinload(TVSeries.episodes)],
    )
    if not series:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Series not found"},
            },
        )
    data = TVSeriesResponse.model_validate(series).model_dump()

    # Episodes
    data["episodes"] = [
        EpisodeResponse.model_validate(e).model_dump() for e in (series.episodes or [])
    ]

    # Resources count and recent
    res_q = await db.execute(
        select(FileResource)
        .where(FileResource.series_id == series_id)
        .order_by(FileResource.published_at.desc())
        .limit(20)
    )
    resources = res_q.scalars().all()
    data["resources"] = [FileResourceResponse.model_validate(r).model_dump() for r in resources]
    data["resource_count"] = len(resources)

    # Download tasks count
    task_cnt = await db.execute(
        select(func.count()).select_from(DownloadTask).where(
            DownloadTask.file_resource.has(FileResource.series_id == series_id)
        )
    )
    data["task_count"] = task_cnt.scalar_one() or 0

    # Agent works referencing this series
    aw_q = await db.execute(
        select(AgentWork).where(AgentWork.series_id == series_id)
    )
    data["agent_work_count"] = len(aw_q.scalars().all())

    return success_response(data)


@router.put("/series/{series_id}")
async def update_series(
    series_id: str,
    body: TVSeriesUpdate,
    db: AsyncSession = Depends(get_db),
):
    series = await db.get(TVSeries, series_id)
    if not series:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Series not found"},
            },
        )
    update_data = body.model_dump(exclude_unset=True)
    # Aliases merge: append new aliases without dedup (per AGENTS.md spec)
    if "aliases" in update_data and update_data["aliases"]:
        existing = set(series.aliases or [])
        new_ones = [a for a in update_data["aliases"] if a not in existing]
        update_data["aliases"] = (series.aliases or []) + new_ones
    for key, value in update_data.items():
        setattr(series, key, value)
    await db.flush()
    await fts_service.upsert_series_fts(db, series)
    await db.refresh(series)
    return success_response(TVSeriesResponse.model_validate(series).model_dump())


@router.delete("/series/{series_id}")
async def delete_series(series_id: str, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import update as sql_update

    from app.models.agent_work import AgentWork
    from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
    from app.models.file_resource import FileResource
    from app.models.pending_decision import PendingDecision
    series = await db.get(TVSeries, series_id)
    if not series:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Series not found"},
            },
        )

    # Constraint check: block if any AgentWork references this series
    aw_cnt = (await db.execute(
        select(func.count()).select_from(AgentWork).where(AgentWork.series_id == series_id)
    )).scalar_one()
    if aw_cnt > 0:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "DELETE_BLOCKED",
                    "message": (
                        f"Cannot delete: {aw_cnt} agent(s) reference this series. "
                        "Remove the agent work subscriptions first."
                    ),
                    "details": {"agent_work_count": aw_cnt},
                },
            },
        )

    # Nullify FKs
    await db.execute(sql_update(FileResource).where(FileResource.series_id == series_id).values(series_id=None))
    await db.execute(sql_update(PendingDecision).where(PendingDecision.series_id == series_id).values(series_id=None))
    await db.execute(
        sql_update(ChannelRawTitleMapping)
        .where(ChannelRawTitleMapping.series_id == series_id)
        .values(series_id=None)
    )
    await db.delete(series)
    await fts_service.delete_series_fts(db, series_id)
    await db.commit()
    return success_response({"deleted": True})
