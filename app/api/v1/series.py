"""TVSeries API routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.responses import JSONResponse

from app.database import get_db
from app.models.series import TVSeries
from app.schemas.series import TVSeriesCreate, TVSeriesUpdate, TVSeriesResponse
from app.schemas.common import success_response, paginated_response

router = APIRouter()


@router.get("/series")
async def list_series(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    total_q = await db.execute(select(func.count()).select_from(TVSeries))
    total = total_q.scalar_one()
    result = await db.execute(
        select(TVSeries).order_by(TVSeries.created_at.desc()).offset(offset).limit(page_size)
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
    await db.refresh(series)
    return success_response(TVSeriesResponse.model_validate(series).model_dump())


@router.get("/series/{series_id}")
async def get_series(series_id: str, db: AsyncSession = Depends(get_db)):
    series = await db.get(TVSeries, series_id)
    if not series:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Series not found"}})
    return success_response(TVSeriesResponse.model_validate(series).model_dump())


@router.put("/series/{series_id}")
async def update_series(
    series_id: str,
    body: TVSeriesUpdate,
    db: AsyncSession = Depends(get_db),
):
    series = await db.get(TVSeries, series_id)
    if not series:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Series not found"}})
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(series, key, value)
    await db.flush()
    await db.refresh(series)
    return success_response(TVSeriesResponse.model_validate(series).model_dump())


@router.delete("/series/{series_id}")
async def delete_series(series_id: str, db: AsyncSession = Depends(get_db)):
    from app.models.file_resource import FileResource
    from app.models.agent_work import AgentWork
    from app.models.pending_decision import PendingDecision
    from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
    from sqlalchemy import update as sql_update
    series = await db.get(TVSeries, series_id)
    if not series:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Series not found"}})
    # Nullify FKs
    await db.execute(sql_update(FileResource).where(FileResource.series_id == series_id).values(series_id=None))
    await db.execute(sql_update(PendingDecision).where(PendingDecision.series_id == series_id).values(series_id=None))
    await db.execute(sql_update(ChannelRawTitleMapping).where(ChannelRawTitleMapping.series_id == series_id).values(series_id=None))
    # Delete agent_works pointing to this series
    res = await db.execute(select(AgentWork).where(AgentWork.series_id == series_id))
    for w in res.scalars().all():
        await db.delete(w)
    await db.delete(series)
    await db.commit()
    return success_response({"deleted": True})
