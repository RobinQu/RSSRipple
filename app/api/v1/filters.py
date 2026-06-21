"""ResourceFilter API routes."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.filter import ResourceFilter
from app.schemas.filter import FilterCreate, FilterUpdate, FilterResponse
from app.schemas.common import success_response
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/agents/{agent_id}/filters")
async def list_filters(agent_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ResourceFilter)
        .where(ResourceFilter.agent_id == agent_id)
        .order_by(ResourceFilter.priority.desc())
    )
    filters = result.scalars().all()
    return success_response([FilterResponse.model_validate(f).model_dump() for f in filters])


@router.post("/agents/{agent_id}/filters", status_code=201)
async def create_filter(
    agent_id: str,
    body: FilterCreate,
    db: AsyncSession = Depends(get_db),
):
    rf = ResourceFilter(agent_id=agent_id, **body.model_dump())
    db.add(rf)
    await db.flush()
    await db.refresh(rf)
    return success_response(FilterResponse.model_validate(rf).model_dump())


@router.put("/agents/{agent_id}/filters/{filter_id}")
async def update_filter(
    agent_id: str,
    filter_id: str,
    body: FilterUpdate,
    db: AsyncSession = Depends(get_db),
):
    rf = await db.get(ResourceFilter, filter_id)
    if not rf or rf.agent_id != agent_id:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Filter not found"}})
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(rf, key, value)
    await db.flush()
    await db.refresh(rf)
    return success_response(FilterResponse.model_validate(rf).model_dump())


@router.delete("/agents/{agent_id}/filters/{filter_id}")
async def delete_filter(
    agent_id: str,
    filter_id: str,
    db: AsyncSession = Depends(get_db),
):
    rf = await db.get(ResourceFilter, filter_id)
    if not rf or rf.agent_id != agent_id:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Filter not found"}})
    await db.delete(rf)
    return success_response({"deleted": True})
