"""FileResource API routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.file_resource import FileResource
from app.schemas.file_resource import FileResourceResponse
from app.schemas.common import success_response, paginated_response
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/channels/{channel_id}/resources")
async def list_resources(
    channel_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    base_q = select(FileResource).where(FileResource.channel_id == channel_id)
    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()
    result = await db.execute(
        base_q.order_by(FileResource.published_at.desc()).offset(offset).limit(page_size)
    )
    resources = result.scalars().all()
    return paginated_response(
        [FileResourceResponse.model_validate(r).model_dump() for r in resources],
        total=total, page=page, page_size=page_size,
    )


@router.get("/resources/{resource_id}")
async def get_resource(resource_id: str, db: AsyncSession = Depends(get_db)):
    resource = await db.get(FileResource, resource_id)
    if not resource:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Resource not found"}})
    return success_response(FileResourceResponse.model_validate(resource).model_dump())
