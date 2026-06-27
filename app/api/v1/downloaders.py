"""DownloaderInstance API routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.downloader import DownloaderInstance
from app.models.download_task import DownloadTask
from app.schemas.downloader import DownloaderCreate, DownloaderUpdate, DownloaderResponse
from app.schemas.download_task import DownloadTaskResponse
from app.schemas.common import success_response, paginated_response
from app.clients.transmission import TransmissionWrapper
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
    from app.models.agent import Agent
    from sqlalchemy import update as sql_update
    dl = await db.get(DownloaderInstance, downloader_id)
    if not dl:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Downloader not found"}})
    # Null agent FKs and pause agents
    await db.execute(
        sql_update(Agent)
        .where(Agent.downloader_id == downloader_id)
        .values(downloader_id=None, status="paused")
    )
    await db.delete(dl)
    await db.commit()
    return success_response({"deleted": True})


@router.get("/downloaders/{downloader_id}/tasks")
async def list_downloader_tasks(
    downloader_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    dl = await db.get(DownloaderInstance, downloader_id)
    if not dl:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Downloader not found"}})
    offset = (page - 1) * page_size
    base_q = select(DownloadTask).where(DownloadTask.downloader_id == downloader_id)
    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()
    result = await db.execute(
        base_q
        .options(selectinload(DownloadTask.file_resource))
        .order_by(DownloadTask.created_at.desc())
        .offset(offset).limit(page_size)
    )
    tasks = result.scalars().all()
    return paginated_response(
        [DownloadTaskResponse.model_validate(t).model_dump() for t in tasks],
        total=total, page=page, page_size=page_size,
    )


@router.get("/downloaders/{downloader_id}/torrents")
async def list_downloader_live_torrents(
    downloader_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Return the live torrent list directly from the Transmission daemon."""
    dl = await db.get(DownloaderInstance, downloader_id)
    if not dl:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Downloader not found"}})
    try:
        wrapper = TransmissionWrapper(dl.url, dl.username, dl.password)
        torrents = await wrapper.list_torrents()
        return success_response(torrents)
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"success": False, "data": None, "error": {"code": "TRANSMISSION_ERROR", "message": str(e)}},
        )


@router.post("/downloaders/{downloader_id}/test")
async def test_downloader(downloader_id: str, db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone

    dl = await db.get(DownloaderInstance, downloader_id)
    if not dl:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Downloader not found"}})

    wrapper = TransmissionWrapper(dl.url, dl.username, dl.password)
    success, detail = await wrapper.test_connection()

    dl.status = "connected" if success else "error"
    dl.last_checked_at = datetime.now(timezone.utc)
    await db.flush()

    if success:
        return success_response({"success": True, "message": detail, "version": detail})
    return success_response({"success": False, "message": detail or "Connection failed", "version": None})
