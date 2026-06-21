"""DownloaderInstance API routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.downloader import DownloaderInstance
from app.schemas.downloader import DownloaderCreate, DownloaderUpdate, DownloaderResponse
from app.schemas.common import success_response, paginated_response
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/downloaders")
async def list_downloaders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    total_q = await db.execute(select(func.count()).select_from(DownloaderInstance))
    total = total_q.scalar_one()
    result = await db.execute(
        select(DownloaderInstance).order_by(DownloaderInstance.created_at.desc()).offset(offset).limit(page_size)
    )
    instances = result.scalars().all()
    return paginated_response(
        [DownloaderResponse.model_validate(d).model_dump() for d in instances],
        total=total, page=page, page_size=page_size,
    )


@router.post("/downloaders", status_code=201)
async def create_downloader(
    body: DownloaderCreate,
    db: AsyncSession = Depends(get_db),
):
    dl = DownloaderInstance(**body.model_dump(exclude={"password"}))
    if body.password:
        dl.password = body.password
    db.add(dl)
    await db.flush()
    await db.refresh(dl)
    return success_response(DownloaderResponse.model_validate(dl).model_dump())


@router.get("/downloaders/{downloader_id}")
async def get_downloader(downloader_id: str, db: AsyncSession = Depends(get_db)):
    dl = await db.get(DownloaderInstance, downloader_id)
    if not dl:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Downloader not found"}})
    return success_response(DownloaderResponse.model_validate(dl).model_dump())


@router.put("/downloaders/{downloader_id}")
async def update_downloader(
    downloader_id: str,
    body: DownloaderUpdate,
    db: AsyncSession = Depends(get_db),
):
    dl = await db.get(DownloaderInstance, downloader_id)
    if not dl:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Downloader not found"}})
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(dl, key, value)
    await db.flush()
    await db.refresh(dl)
    return success_response(DownloaderResponse.model_validate(dl).model_dump())


@router.delete("/downloaders/{downloader_id}")
async def delete_downloader(downloader_id: str, db: AsyncSession = Depends(get_db)):
    dl = await db.get(DownloaderInstance, downloader_id)
    if not dl:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Downloader not found"}})
    await db.delete(dl)
    return success_response({"deleted": True})


@router.post("/downloaders/{downloader_id}/test")
async def test_downloader(downloader_id: str, db: AsyncSession = Depends(get_db)):
    dl = await db.get(DownloaderInstance, downloader_id)
    if not dl:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Downloader not found"}})
    # TODO: Actually test connection via transmission-rpc
    return success_response({"success": True, "message": "Connection test pending implementation", "version": None})
