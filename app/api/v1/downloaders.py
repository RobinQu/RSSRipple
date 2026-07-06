"""DownloaderInstance API routes."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clients.downloader import get_downloader_client
from app.database import get_db
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.schemas.common import paginated_response, success_response
from app.schemas.download_task import DownloadTaskResponse
from app.schemas.downloader import DownloaderCreate, DownloaderResponse, DownloaderUpdate
from app.utils.download_paths import DownloadPathError
from app.utils.time import utcnow

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
    try:
        payload = body.model_dump(exclude={"password"})
    except DownloadPathError as e:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "data": None,
                "error": {"code": "VALIDATION_ERROR", "message": str(e)},
                "meta": {},
            },
        )
    dl = DownloaderInstance(**payload)
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
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Downloader not found"},
            },
        )
    return success_response(DownloaderResponse.model_validate(dl).model_dump())


@router.put("/downloaders/{downloader_id}")
async def update_downloader(
    downloader_id: str,
    body: DownloaderUpdate,
    db: AsyncSession = Depends(get_db),
):
    dl = await db.get(DownloaderInstance, downloader_id)
    if not dl:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Downloader not found"},
            },
        )
    try:
        update_data = body.model_dump(exclude_unset=True)
    except DownloadPathError as e:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "data": None,
                "error": {"code": "VALIDATION_ERROR", "message": str(e)},
                "meta": {},
            },
        )
    for key, value in update_data.items():
        setattr(dl, key, value)
    await db.flush()
    await db.refresh(dl)
    return success_response(DownloaderResponse.model_validate(dl).model_dump())


@router.delete("/downloaders/{downloader_id}")
async def delete_downloader(downloader_id: str, db: AsyncSession = Depends(get_db)):
    from app.models.agent import Agent
    from app.models.download_task import DownloadTask
    dl = await db.get(DownloaderInstance, downloader_id)
    if not dl:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Downloader not found"},
            },
        )
    # Surface the specific agents still bound to this downloader so the
    # frontend can offer a "jump to agent" affordance instead of just a
    # generic 409.
    linked_agents = (await db.execute(
        select(Agent.id, Agent.name).where(Agent.downloader_id == downloader_id)
    )).all()
    if linked_agents:
        agents_payload = [{"id": aid, "name": name} for aid, name in linked_agents]
        agent_names = ", ".join(a["name"] for a in agents_payload)
        return JSONResponse(status_code=409, content={
            "success": False,
            "data": None,
            "error": {
                "code": "CONFLICT",
                "message": (
                    f"Downloader is still used by {len(agents_payload)} "
                    f"agent(s): {agent_names}"
                ),
                "details": {"agents": agents_payload},
            },
            "meta": {},
        })
    # Cascade-delete associated DownloadTasks before removing the downloader
    linked_tasks = await db.execute(
        select(DownloadTask).where(DownloadTask.downloader_id == downloader_id)
    )
    for task in linked_tasks.scalars().all():
        task.status = "cancelled"
        await db.delete(task)
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
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Downloader not found"},
            },
        )
    offset = (page - 1) * page_size
    base_q = select(DownloadTask).where(DownloadTask.downloader_id == downloader_id)
    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()
    result = await db.execute(
        base_q
        .options(
            selectinload(DownloadTask.file_resource),
            selectinload(DownloadTask.agent),
        )
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
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Downloader not found"},
            },
        )
    try:
        wrapper = get_downloader_client(dl)
        torrents = await wrapper.list_torrents()
        return success_response(torrents)
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"success": False, "data": None, "error": {"code": "TRANSMISSION_ERROR", "message": str(e)}},
        )


@router.post("/downloaders/{downloader_id}/test")
async def test_downloader(downloader_id: str, db: AsyncSession = Depends(get_db)):

    dl = await db.get(DownloaderInstance, downloader_id)
    if not dl:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Downloader not found"},
            },
        )

    wrapper = get_downloader_client(dl)
    success, detail = await wrapper.test_connection()
    version = detail if success else None

    dl.status = "connected" if success else "error"
    dl.last_checked_at = utcnow()
    free_space = None
    if success:
        try:
            free_space = await wrapper.free_space(dl.download_dir)
        except Exception as e:
            success = False
            detail = f"{detail}; download_dir check failed: {e}" if detail else f"download_dir check failed: {e}"
            dl.status = "error"
    await db.flush()

    if success:
        return success_response({"success": True, "message": detail, "version": version, "free_space": free_space})
    return success_response(
        {"success": False, "message": detail or "Connection failed", "version": version, "free_space": free_space}
    )
