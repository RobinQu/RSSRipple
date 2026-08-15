"""DownloaderInstance API routes."""

from types import SimpleNamespace

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clients.downloader import get_downloader_client
from app.database import get_db
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.storage_volume import StorageVolume
from app.schemas.common import paginated_response, success_response
from app.schemas.download_task import DownloadTaskResponse
from app.schemas.downloader import (
    DownloaderCreate,
    DownloaderResponse,
    DownloaderTestRequest,
    DownloaderUpdate,
)
from app.services.volume_service import check_mount
from app.utils.download_paths import DownloadPathError
from app.utils.time import utcnow

router = APIRouter()


def _join(base: str, extra: str) -> str:
    """Join two probe detail fragments into a single message (skips empties)."""
    return f"{base}; {extra}" if base else extra


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": message},
            "meta": {},
        },
    )


async def _validate_volume_binding(
    db: AsyncSession, volume_id: str | None, volume_subpath: str | None
) -> JSONResponse | None:
    """卷绑定校验：volume_subpath 必须依附 volume_id（422）；volume_id 必须
    指向存在的 StorageVolume（404）。"""
    if volume_subpath and not volume_id:
        return _error(
            422, "VALIDATION_ERROR",
            "volume_subpath 必须依附 volume_id（绑定不完整）",
        )
    if volume_id is not None:
        if await db.get(StorageVolume, volume_id) is None:
            return _error(404, "NOT_FOUND", "Storage volume not found")
    return None


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
    if err := await _validate_volume_binding(
        db, payload.get("volume_id"), payload.get("volume_subpath")
    ):
        return err
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
    # 卷绑定校验按合并后的目标状态进行（允许只更新子路径或只换卷）。
    merged_volume_id = update_data.get("volume_id", dl.volume_id)
    merged_subpath = update_data.get("volume_subpath", dl.volume_subpath)
    if err := await _validate_volume_binding(db, merged_volume_id, merged_subpath):
        return err
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
async def test_downloader(
    downloader_id: str,
    body: DownloaderTestRequest | None = None,
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

    # The edit form probes the *unsaved* form values: any field present in the
    # request body overrides the stored config for this probe only. A missing
    # field (e.g. blank password) falls back to the stored value.
    explicit = body.model_fields_set if body is not None else set()
    has_overrides = bool(explicit)
    if has_overrides:
        target = SimpleNamespace(
            id=dl.id,
            name=dl.name,
            type=dl.type,
            url=body.url or dl.url,
            username=body.username if body.username is not None else dl.username,
            password=body.password if body.password is not None else dl.password,
            download_dir=body.download_dir or dl.download_dir,
            # volume_id / volume_subpath: null is a meaningful value (unbind),
            # so distinguish "not sent" (keep stored) from an explicit null.
            volume_id=body.volume_id if "volume_id" in explicit else dl.volume_id,
            volume_subpath=(
                body.volume_subpath if "volume_subpath" in explicit else dl.volume_subpath
            ),
        )
    else:
        target = dl

    wrapper = get_downloader_client(target)
    success, detail = await wrapper.test_connection()
    version = detail if success else None

    free_space = None
    if success:
        try:
            free_space = await wrapper.free_space(target.download_dir)
        except Exception as e:
            success = False
            detail = _join(detail, f"download_dir check failed: {e}")

    # Volume binding validity (docs/design/file-organization.md「统一路径解析」):
    # the process-side download root must exist and be readable + writable.
    volume_check = None
    if getattr(target, "volume_id", None):
        volume = await db.get(StorageVolume, target.volume_id)
        if volume is None:
            volume_check = {"exists": False, "readable": False, "writable": False}
            success = False
            detail = _join(detail, "bound storage volume no longer exists")
        else:
            base = volume.mount_path.rstrip("/")
            subpath = (getattr(target, "volume_subpath", None) or "").strip("/")
            resolved = f"{base}/{subpath}" if subpath else base
            volume_check = check_mount(resolved)
            if not volume_check["exists"]:
                success = False
                detail = _join(detail, f"volume path does not exist: {resolved}")
            elif not volume_check["readable"] or not volume_check["writable"]:
                success = False
                detail = _join(
                    detail, f"volume path is not readable/writable: {resolved}"
                )

    status = "connected" if success else "error"

    # Persist the health status only when probing the *stored* config — a
    # form-values probe says nothing about the saved instance.
    if not has_overrides:
        dl.status = status
        dl.last_checked_at = utcnow()
        await db.flush()

    return success_response({
        "success": success,
        "message": detail or ("Connection failed" if not success else ""),
        "version": version,
        "free_space": free_space,
        "volume_check": volume_check,
    })
