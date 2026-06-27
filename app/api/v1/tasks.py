"""DownloadTask API routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from fastapi.responses import JSONResponse

from app.database import get_db
from app.models.download_task import DownloadTask
from app.schemas.download_task import DownloadTaskResponse, TaskActionResponse
from app.schemas.common import success_response, paginated_response

router = APIRouter()


async def _apply_torrent_action(db, task: DownloadTask, action: str, delete_data: bool = False) -> bool:
    """Call Transmission for pause/resume/retry/delete actions."""
    from app.models.downloader import DownloaderInstance
    from app.clients.transmission import TransmissionWrapper
    if not task.downloader_id:
        return False
    downloader = await db.get(DownloaderInstance, task.downloader_id)
    if not downloader:
        return False
    wrapper = TransmissionWrapper(url=downloader.url, username=downloader.username, password=downloader.password)
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
            resource = await db.get(FileResource, task.file_resource_id)
            if not resource:
                return False
            result = await wrapper.add_torrent(resource.torrent_url, download_dir=task.download_dir)
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


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(
        DownloadTask, task_id,
        options=[selectinload(DownloadTask.file_resource), selectinload(DownloadTask.agent)],
    )
    if not task:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Task not found"}, "meta": {}})
    return success_response(DownloadTaskResponse.model_validate(task).model_dump())


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Task not found"}, "meta": {}})
    ok = await _apply_torrent_action(db, task, "pause")
    if ok:
        task.status = "paused"
    await db.flush()
    await db.commit()
    return success_response(TaskActionResponse(id=task.id, status=task.status, message="paused" if ok else "failed").model_dump())


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Task not found"}, "meta": {}})
    ok = await _apply_torrent_action(db, task, "resume")
    if ok:
        task.status = "queued"
    await db.flush()
    await db.commit()
    return success_response(TaskActionResponse(id=task.id, status=task.status, message="resumed" if ok else "failed").model_dump())


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Task not found"}, "meta": {}})
    ok = await _apply_torrent_action(db, task, "retry")
    await db.flush()
    await db.commit()
    return success_response(TaskActionResponse(id=task.id, status=task.status, message="retried" if ok else "failed").model_dump())


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    delete_data: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Task not found"}, "meta": {}})
    if task.transmission_torrent_id:
        await _apply_torrent_action(db, task, "remove", delete_data=delete_data)
    task.status = "cancelled"
    await db.flush()
    await db.commit()
    return success_response({"deleted": True})
