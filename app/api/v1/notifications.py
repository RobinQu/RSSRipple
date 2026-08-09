"""Download notification API routes.

Read endpoints serve the UI (Agent detail → 通知记录 tab); the start/ack/fail
callback endpoints serve the external consumer (vault-organizer) and
authenticate with the per-Agent callback token issued at webhook
registration (Bearer).
"""

import secrets

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.agent import Agent
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.schemas.common import paginated_response, success_response
from app.schemas.notification import (
    BackfillRequest,
    FailRequest,
    NotificationDetail,
    NotificationListItem,
    WebhookRegisterRequest,
)
from app.services.notify_service import backfill_notifications, reset_for_retry
from app.utils.time import utcnow

router = APIRouter()


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": message, "details": None},
            "meta": {},
        },
    )


def _check_callback_token(
    request: Request, agent: Agent | None
) -> JSONResponse | None:
    """Authenticate a consumer callback against the owning Agent's token."""
    if agent is None or not agent.notify_webhook_token:
        return _error(
            503, "CALLBACK_TOKEN_NOT_CONFIGURED",
            "该 Agent 未注册 webhook，回调端点不可用",
        )
    auth = request.headers.get("authorization", "")
    if not secrets.compare_digest(auth, f"Bearer {agent.notify_webhook_token}"):
        return _error(401, "UNAUTHORIZED", "回调 token 无效")
    return None


def _webhook_status(agent: Agent) -> dict:
    return {
        "registered": bool(agent.notify_webhook_url or agent.notify_webhook_mock),
        "url": agent.notify_webhook_url,
        "mock": agent.notify_webhook_mock,
        "token": agent.notify_webhook_token,
    }


async def _get_agent_or_404(db: AsyncSession, agent_id: str) -> Agent | JSONResponse:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        return _error(404, "NOT_FOUND", "Agent not found")
    return agent


async def _get_notification_or_404(
    db: AsyncSession, notification_id: str
) -> DownloadNotification | JSONResponse:
    n = await db.get(DownloadNotification, notification_id)
    if n is None:
        return _error(404, "NOT_FOUND", "Notification not found")
    return n


# ---------------------------------------------------------------------------
# Agent-scoped: queue listing, webhook registration, backfill
# ---------------------------------------------------------------------------


@router.get("/agents/{agent_id}/notifications")
async def list_notifications(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    agent = await _get_agent_or_404(db, agent_id)
    if isinstance(agent, JSONResponse):
        return agent
    base_q = select(DownloadNotification).where(
        DownloadNotification.agent_id == agent_id
    )
    if status:
        base_q = base_q.where(DownloadNotification.status == status)
    total = (
        await db.execute(select(func.count()).select_from(base_q.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base_q.order_by(DownloadNotification.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return paginated_response(
        [NotificationListItem.model_validate(n).model_dump() for n in rows],
        total, page, page_size,
    )


@router.get("/agents/{agent_id}/webhook")
async def get_webhook(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await _get_agent_or_404(db, agent_id)
    if isinstance(agent, JSONResponse):
        return agent
    return success_response(_webhook_status(agent))


@router.put("/agents/{agent_id}/webhook")
async def register_webhook(
    agent_id: str,
    body: WebhookRegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """Register/update the webhook. Each registration issues a fresh callback
    token — the consumer must be reconfigured with the new token."""
    agent = await _get_agent_or_404(db, agent_id)
    if isinstance(agent, JSONResponse):
        return agent
    if not body.mock and not body.url:
        return _error(422, "VALIDATION_ERROR", "非 mock webhook 必须提供 url")
    agent.notify_webhook_mock = body.mock
    agent.notify_webhook_url = None if body.mock else body.url
    agent.notify_webhook_token = secrets.token_urlsafe(32)
    await db.commit()
    return success_response(_webhook_status(agent))


@router.delete("/agents/{agent_id}/webhook")
async def unregister_webhook(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await _get_agent_or_404(db, agent_id)
    if isinstance(agent, JSONResponse):
        return agent
    agent.notify_webhook_url = None
    agent.notify_webhook_mock = False
    agent.notify_webhook_token = None
    await db.commit()
    return success_response(_webhook_status(agent))


@router.post("/agents/{agent_id}/notifications/backfill")
async def backfill_agent_notifications(
    agent_id: str,
    body: BackfillRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create notifications for completed tasks that never had one.

    ``since=null`` checks from the earliest completed task. Delivery is
    handled by the per-minute delivery loop (natural rate limiting).
    """
    agent = await _get_agent_or_404(db, agent_id)
    if isinstance(agent, JSONResponse):
        return agent
    created = await backfill_notifications(db, agent_id, body.since)
    return success_response({"created": created})


# ---------------------------------------------------------------------------
# Notification-scoped: detail, retry (UI), start/ack/fail (consumer callbacks)
# ---------------------------------------------------------------------------


@router.get("/notifications/{notification_id}")
async def get_notification(
    notification_id: str, db: AsyncSession = Depends(get_db)
):
    n = await _get_notification_or_404(db, notification_id)
    if isinstance(n, JSONResponse):
        return n
    return success_response(NotificationDetail.model_validate(n).model_dump())


@router.post("/notifications/{notification_id}/retry")
async def retry_notification(
    notification_id: str, db: AsyncSession = Depends(get_db)
):
    """Manual UI retry: reset to pending and make it due immediately."""
    n = await _get_notification_or_404(db, notification_id)
    if isinstance(n, JSONResponse):
        return n
    if n.status == "done":
        return _error(409, "INVALID_STATE", "已消费成功的通知不能重试")
    reset_for_retry(n)
    await db.commit()
    await db.refresh(n)  # pick up server-side onupdate columns (updated_at)
    return success_response(NotificationDetail.model_validate(n).model_dump())


@router.post("/notifications/{notification_id}/start")
async def start_notification(
    notification_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    n = await _get_notification_or_404(db, notification_id)
    if isinstance(n, JSONResponse):
        return n
    denied = _check_callback_token(request, await db.get(Agent, n.agent_id))
    if denied is not None:
        return denied
    if n.status == "pending":
        n.status = "processing"
        await db.commit()
        await db.refresh(n)  # pick up server-side onupdate columns (updated_at)
    elif n.status != "processing":
        return _error(
            409, "INVALID_STATE",
            f"当前状态 {n.status} 不能标记为 processing",
        )
    return success_response(NotificationDetail.model_validate(n).model_dump())


@router.post("/notifications/{notification_id}/ack")
async def ack_notification(
    notification_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Consumer confirms successful processing → remove the torrent."""
    n = await _get_notification_or_404(db, notification_id)
    if isinstance(n, JSONResponse):
        return n
    denied = _check_callback_token(request, await db.get(Agent, n.agent_id))
    if denied is not None:
        return denied
    if n.status not in ("pending", "processing"):
        return _error(
            409, "INVALID_STATE", f"当前状态 {n.status} 不能 ack"
        )
    n.status = "done"
    n.processed_at = utcnow()
    n.error_message = None

    task = await db.get(DownloadTask, n.download_task_id)
    if (
        task is not None
        and task.transmission_torrent_id is not None
        and task.downloader_id
    ):
        downloader = await db.get(DownloaderInstance, task.downloader_id)
        if downloader is not None:
            from app.clients.downloader import get_downloader_client

            wrapper = get_downloader_client(downloader)
            try:
                ok = await wrapper.remove_torrent(
                    task.transmission_torrent_id, delete_data=False
                )
                if not ok:
                    n.error_message = "warning: torrent 删除失败（可能已被移除）"
            except Exception as e:
                n.error_message = f"warning: torrent 删除失败: {e}"[:2000]
    await db.commit()
    await db.refresh(n)  # pick up server-side onupdate columns (updated_at)
    return success_response(NotificationDetail.model_validate(n).model_dump())


@router.post("/notifications/{notification_id}/fail")
async def fail_notification(
    notification_id: str,
    body: FailRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    n = await _get_notification_or_404(db, notification_id)
    if isinstance(n, JSONResponse):
        return n
    denied = _check_callback_token(request, await db.get(Agent, n.agent_id))
    if denied is not None:
        return denied
    if n.status == "done":
        return _error(409, "INVALID_STATE", "已消费成功的通知不能标记失败")
    n.status = "failed"
    n.error_message = body.error[:2000]
    n.processed_at = utcnow()
    await db.commit()
    await db.refresh(n)  # pick up server-side onupdate columns (updated_at)
    return success_response(NotificationDetail.model_validate(n).model_dump())
