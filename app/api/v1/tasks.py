"""DownloadTask API routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.download_task import DownloadTask
from app.schemas.download_task import DownloadTaskResponse
from app.schemas.common import success_response, paginated_response
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/agents/{agent_id}/tasks")
async def list_agent_tasks(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    base_q = select(DownloadTask).where(DownloadTask.agent_id == agent_id)
    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()
    result = await db.execute(
        base_q.order_by(DownloadTask.created_at.desc()).offset(offset).limit(page_size)
    )
    tasks = result.scalars().all()
    return paginated_response(
        [DownloadTaskResponse.model_validate(t).model_dump() for t in tasks],
        total=total, page=page, page_size=page_size,
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Task not found"}})
    return success_response(DownloadTaskResponse.model_validate(task).model_dump())


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Task not found"}})
    # TODO: Pause via Transmission RPC
    task.status = "paused"
    await db.flush()
    return success_response({"id": task.id, "status": "paused", "message": "Task paused"})


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Task not found"}})
    # TODO: Resume via Transmission RPC
    task.status = "queued"
    await db.flush()
    return success_response({"id": task.id, "status": "queued", "message": "Task resumed"})


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Task not found"}})
    # TODO: Retry via Transmission RPC
    task.status = "pending"
    task.retry_count += 1
    task.error_message = None
    await db.flush()
    return success_response({"id": task.id, "status": "pending", "message": "Task retried"})


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(DownloadTask, task_id)
    if not task:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Task not found"}})
    # TODO: Remove from Transmission
    task.status = "cancelled"
    await db.flush()
    return success_response({"deleted": True})
