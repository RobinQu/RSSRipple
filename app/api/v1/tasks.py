"""DownloadTask API routes."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.download_task import DownloadTask
from app.schemas.common import paginated_response, success_response
from app.schemas.download_task import (
    BatchTaskRetryRequest,
    BatchTaskRetryResponse,
    DownloadTaskResponse,
    ManualTaskCreate,
    TaskActionResponse,
)

router = APIRouter()

TASK_STATUSES = (
    "pending", "queued", "downloading", "paused",
    "completed", "error", "cancelled",
)


async def _apply_torrent_action(db, task: DownloadTask, action: str, delete_data: bool = False) -> bool:
    """Call Transmission for pause/resume/retry/delete actions."""
    from app.clients.downloader import get_downloader_client
    from app.models.downloader import DownloaderInstance
    if not task.downloader_id:
        return False
    downloader = await db.get(DownloaderInstance, task.downloader_id)
    if not downloader:
        return False
    wrapper = get_downloader_client(downloader)
    try:
        if action == "pause":
            return await wrapper.pause_torrent(task.transmission_torrent_id)
        elif action == "resume":
            return await wrapper.resume_torrent(task.transmission_torrent_id)
        elif action == "remove":
            return await wrapper.remove_torrent(task.transmission_torrent_id, delete_data=delete_data)
        elif action == "retry":
            # Re-add the torrent
            from app.models.file_resource import FileResource
            from app.services.agent_service import resolve_torrent_payload
            resource = await db.get(FileResource, task.file_resource_id)
            if not resource:
                return False
            result = await wrapper.add_torrent(
                resolve_torrent_payload(resource), download_dir=task.download_dir
            )
            task.transmission_torrent_id = result["torrent_id"]
            task.status = "downloading"
            task.error_message = None
            task.retry_count += 1
            return True
    except Exception as e:
        task.error_message = str(e)[:2000]
        task.status = "error"
        return False
    return False


@router.get("/agents/{agent_id}/tasks")
async def list_agent_tasks(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    base_q = select(DownloadTask).where(DownloadTask.agent_id == agent_id)
    if status:
        base_q = base_q.where(DownloadTask.status == status)
    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()
    result = await db.execute(
        base_q.options(selectinload(DownloadTask.file_resource), selectinload(DownloadTask.agent))
        .order_by(DownloadTask.created_at.desc())
        .offset(offset).limit(page_size)
    )
    tasks = result.scalars().all()
    return paginated_response(
        [DownloadTaskResponse.model_validate(t).model_dump() for t in tasks],
        total=total, page=page, page_size=page_size,
    )


@router.get("/tasks")
async def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    downloader_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Global download task list (newest first), for external consumers."""
    if status is not None and status not in TASK_STATUSES:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "data": None,
                "error": {"code": "VALIDATION_ERROR", "message": f"invalid status: {status}"},
                "meta": {},
            },
        )
    offset = (page - 1) * page_size
    base_q = select(DownloadTask)
    if downloader_id:
        base_q = base_q.where(DownloadTask.downloader_id == downloader_id)
    if agent_id:
        base_q = base_q.where(DownloadTask.agent_id == agent_id)
    if status:
        base_q = base_q.where(DownloadTask.status == status)
    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()
    result = await db.execute(
        base_q.options(selectinload(DownloadTask.file_resource), selectinload(DownloadTask.agent))
        .order_by(DownloadTask.created_at.desc())
        .offset(offset).limit(page_size)
    )
    tasks = result.scalars().all()
    return paginated_response(
        [DownloadTaskResponse.model_validate(t).model_dump() for t in tasks],
        total=total, page=page, page_size=page_size,
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(
        DownloadTask, task_id,
        options=[selectinload(DownloadTask.file_resource), selectinload(DownloadTask.agent)],
    )
    if not task:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Task not found"},
                "meta": {},
            },
        )
    return success_response(DownloadTaskResponse.model_validate(task).model_dump())


@router.post("/tasks", status_code=201)
async def create_task(body: ManualTaskCreate, db: AsyncSession = Depends(get_db)):
    """Manually create a download task from a FileResource, bypassing agents."""
    from app.models.downloader import DownloaderInstance
    from app.models.file_resource import FileResource
    from app.services.agent_service import create_and_submit_task

    resource = await db.get(FileResource, body.resource_id)
    if not resource:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Resource not found"},
                "meta": {},
            },
        )
    downloader = await db.get(DownloaderInstance, body.downloader_id)
    if not downloader:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Downloader not found"},
                "meta": {},
            },
        )
    task = await create_and_submit_task(
        resource,
        downloader,
        db,
        agent_id=None,
        download_dir=downloader.download_dir,
    )
    await db.commit()
    return success_response(DownloadTaskResponse.model_validate(task).model_dump())


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Task not found"},
                "meta": {},
            },
        )
    ok = await _apply_torrent_action(db, task, "pause")
    if ok:
        task.status = "paused"
    await db.flush()
    await db.commit()
    return success_response(
        TaskActionResponse(id=task.id, status=task.status, message="paused" if ok else "failed").model_dump()
    )


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Task not found"},
                "meta": {},
            },
        )
    ok = await _apply_torrent_action(db, task, "resume")
    if ok:
        task.status = "queued"
    await db.flush()
    await db.commit()
    return success_response(
        TaskActionResponse(id=task.id, status=task.status, message="resumed" if ok else "failed").model_dump()
    )


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Task not found"},
                "meta": {},
            },
        )
    ok = await _apply_torrent_action(db, task, "retry")
    await db.flush()
    await db.commit()
    return success_response(
        TaskActionResponse(id=task.id, status=task.status, message="retried" if ok else "failed").model_dump()
    )


@router.post("/agents/{agent_id}/tasks/batch-retry")
async def batch_retry_tasks(
    agent_id: str,
    body: BatchTaskRetryRequest,
    db: AsyncSession = Depends(get_db),
):
    """Retry many tasks of an agent at once.

    Only ``error``/``paused`` tasks participate (same condition as the
    row-level retry button); other statuses are skipped. ``task_ids=None``
    retries every retryable task of the agent.
    """
    q = select(DownloadTask).where(
        DownloadTask.agent_id == agent_id,
        DownloadTask.status.in_(["error", "paused"]),
    )
    if body.task_ids:
        q = q.where(DownloadTask.id.in_(body.task_ids))
    rows = (await db.execute(q)).scalars().all()

    resp = BatchTaskRetryResponse()
    for task in rows:
        resp.processed += 1
        try:
            ok = await _apply_torrent_action(db, task, "retry")
            if ok:
                resp.retried += 1
            else:
                resp.failed += 1
                resp.errors.append(f"{task.id}: {task.error_message or 'retry failed'}")
        except Exception as e:  # noqa: BLE001
            resp.failed += 1
            resp.errors.append(f"{task.id}: {e}")
    await db.commit()
    return success_response(resp.model_dump())


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    delete_data: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Task not found"},
                "meta": {},
            },
        )
    if delete_data:
        # 删除数据的清理不走共用逻辑（整理语义固定保留数据）。
        if task.transmission_torrent_id:
            await _apply_torrent_action(db, task, "remove", delete_data=True)
        task.status = "cancelled"
    else:
        # 与内置整理子系统（organize）执行后清理同一实现：
        # app/services/task_cleanup.py::delete_task_after_organize。
        from app.services.task_cleanup import delete_task_after_organize

        await delete_task_after_organize(db, task_id)
    await db.flush()
    await db.commit()
    return success_response({"deleted": True})
