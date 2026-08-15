"""Download notification API routes.

Read endpoints serve the UI (Agent detail → 通知记录 tab). Delivery state
lives on per-webhook ``WebhookDelivery`` rows; a notification's displayed
status is aggregated from its deliveries. Consumer callbacks (start/ack/fail)
were removed with the single-webhook model — delivery is fire-and-forget
with retries; consumers simply receive the webhook POST.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.agent import Agent
from app.models.agent_webhook import AgentWebhook
from app.models.download_notification import DownloadNotification
from app.models.webhook_delivery import WebhookDelivery
from app.schemas.common import paginated_response, success_response
from app.schemas.notification import (
    NotificationPayload,
    NotificationRetryRequest,
    RegenerateRequest,
    RetryRequest,
    WebhookCreateRequest,
    WebhookOut,
    WebhookUpdateRequest,
)
from app.services.notify_service import (
    ensure_deliveries,
    regenerate_notifications,
    reset_deliveries_for_retry,
)

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


async def _get_webhook_or_404(
    db: AsyncSession, agent_id: str, webhook_id: str
) -> AgentWebhook | JSONResponse:
    w = (
        await db.execute(
            select(AgentWebhook).where(
                AgentWebhook.id == webhook_id,
                AgentWebhook.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    if w is None:
        return _error(404, "NOT_FOUND", "Webhook not found")
    return w


# ---------------------------------------------------------------------------
# Aggregation helpers — the single source of truth for a notification's
# displayed status. The list endpoint's SQL filter mirrors these rules via
# correlated EXISTS subqueries (keep the two in sync).
# ---------------------------------------------------------------------------


def _aggregate_status(deliveries: list[WebhookDelivery]) -> str:
    if not deliveries or any(d.status == "pending" for d in deliveries):
        return "pending"
    if all(d.status == "done" for d in deliveries):
        return "done"
    return "failed"


def _delivery_summary(deliveries: list[WebhookDelivery]) -> dict:
    return {
        "total": len(deliveries),
        "done": sum(1 for d in deliveries if d.status == "done"),
        "failed": sum(1 for d in deliveries if d.status == "failed"),
        "pending": sum(1 for d in deliveries if d.status == "pending"),
    }


def _notification_title(payload) -> str | None:
    """Raw file title from the frozen payload (resource.title_raw, fallback
    task.torrent_name). Payloads from older versions may lack either key."""
    if not isinstance(payload, dict):
        return None
    resource = payload.get("resource")
    if isinstance(resource, dict) and resource.get("title_raw"):
        return resource["title_raw"]
    task = payload.get("task")
    if isinstance(task, dict) and task.get("torrent_name"):
        return task["torrent_name"]
    return None


def _list_item(n: DownloadNotification) -> dict:
    return {
        "id": n.id,
        "agent_id": n.agent_id,
        "download_task_id": n.download_task_id,
        "title_raw": _notification_title(n.payload),
        "status": _aggregate_status(n.deliveries),
        "delivery_summary": _delivery_summary(n.deliveries),
        "created_at": n.created_at,
        "updated_at": n.updated_at,
    }


def _delivery_out(d: WebhookDelivery) -> dict:
    return {
        "id": d.id,
        "webhook_id": d.webhook_id,
        "webhook_url": d.webhook.url if d.webhook else None,
        "status": d.status,
        "attempt_count": d.attempt_count,
        "error_message": d.error_message,
        "delivered_at": d.delivered_at,
        "next_attempt_at": d.next_attempt_at,
        "created_at": d.created_at,
    }


def _deliveries_exist(*statuses: str):
    """Correlated EXISTS subquery: the outer notification has a delivery
    (optionally restricted to the given statuses)."""
    q = select(WebhookDelivery.id).where(
        WebhookDelivery.notification_id == DownloadNotification.id
    )
    if statuses:
        q = q.where(WebhookDelivery.status.in_(statuses))
    return q.exists()


# ---------------------------------------------------------------------------
# Agent-scoped: queue listing, webhook collection, regenerate
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
    if status is not None and status not in ("pending", "done", "failed"):
        return _error(422, "VALIDATION_ERROR", f"未知状态: {status}")
    base_q = select(DownloadNotification).where(
        DownloadNotification.agent_id == agent_id
    )
    # Filter on the aggregated status via delivery subqueries so pagination
    # stays correct (mirrors _aggregate_status):
    #   pending = no deliveries OR any pending delivery
    #   done    = has deliveries AND none pending/failed
    #   failed  = some failed AND none pending
    if status == "pending":
        base_q = base_q.where(
            ~_deliveries_exist() | _deliveries_exist("pending")
        )
    elif status == "done":
        base_q = base_q.where(
            _deliveries_exist() & ~_deliveries_exist("pending", "failed")
        )
    elif status == "failed":
        base_q = base_q.where(
            _deliveries_exist("failed") & ~_deliveries_exist("pending")
        )
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
        [_list_item(n) for n in rows], total, page, page_size,
    )


@router.get("/agents/{agent_id}/webhooks")
async def list_webhooks(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await _get_agent_or_404(db, agent_id)
    if isinstance(agent, JSONResponse):
        return agent
    rows = (
        await db.execute(
            select(AgentWebhook)
            .where(AgentWebhook.agent_id == agent_id)
            .order_by(AgentWebhook.created_at.asc())
        )
    ).scalars().all()
    return success_response(
        [WebhookOut.model_validate(w).model_dump() for w in rows]
    )


@router.post("/agents/{agent_id}/webhooks", status_code=201)
async def create_webhook(
    agent_id: str,
    body: WebhookCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    agent = await _get_agent_or_404(db, agent_id)
    if isinstance(agent, JSONResponse):
        return agent
    webhook = AgentWebhook(
        agent_id=agent_id, url=body.url, mock=body.mock, enabled=body.enabled
    )
    db.add(webhook)
    await db.flush()
    # Fan out immediately so the new webhook receives the existing backlog
    # instead of waiting for the next scheduler tick.
    await ensure_deliveries(db, agent_id=agent_id)
    await db.commit()
    await db.refresh(webhook)
    return success_response(WebhookOut.model_validate(webhook).model_dump())


@router.put("/agents/{agent_id}/webhooks/{webhook_id}")
async def update_webhook(
    agent_id: str,
    webhook_id: str,
    body: WebhookUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    agent = await _get_agent_or_404(db, agent_id)
    if isinstance(agent, JSONResponse):
        return agent
    webhook = await _get_webhook_or_404(db, agent_id, webhook_id)
    if isinstance(webhook, JSONResponse):
        return webhook
    if body.url is not None:
        webhook.url = body.url
    if body.mock is not None:
        webhook.mock = body.mock
    if body.enabled is not None:
        webhook.enabled = body.enabled
    # Re-enabling (or any update) picks up the backlog on the next tick;
    # fan out now so it happens immediately.
    await ensure_deliveries(db, agent_id=agent_id)
    await db.commit()
    await db.refresh(webhook)
    return success_response(WebhookOut.model_validate(webhook).model_dump())


@router.delete("/agents/{agent_id}/webhooks/{webhook_id}")
async def delete_webhook(
    agent_id: str, webhook_id: str, db: AsyncSession = Depends(get_db)
):
    agent = await _get_agent_or_404(db, agent_id)
    if isinstance(agent, JSONResponse):
        return agent
    webhook = await _get_webhook_or_404(db, agent_id, webhook_id)
    if isinstance(webhook, JSONResponse):
        return webhook
    await db.delete(webhook)
    await db.commit()
    return success_response({"deleted": True})


@router.post("/agents/{agent_id}/notifications/regenerate")
async def regenerate_agent_notifications(
    agent_id: str,
    body: RegenerateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Regenerate notifications: re-run the full generation chain for every
    completed task of the agent (optionally ``completed_at >= since``;
    ``since=null`` from the earliest). Missing notifications are created,
    existing ones have their payload rebuilt and their deliveries reset for
    re-delivery. Delivery itself is handled by the per-minute loop (natural
    rate limiting).
    """
    agent = await _get_agent_or_404(db, agent_id)
    if isinstance(agent, JSONResponse):
        return agent
    stats = await regenerate_notifications(db, agent_id, body.since)
    return success_response(stats)


# ---------------------------------------------------------------------------
# Notification-scoped: detail, retry (UI)
# ---------------------------------------------------------------------------


@router.get("/notifications/{notification_id}")
async def get_notification(
    notification_id: str, db: AsyncSession = Depends(get_db)
):
    """Notification detail, including the frozen snapshot ``payload``.

    Payload contract (see docs/design/notifications.md):
    ``{notification_id, agent, task, resource, work, files}``. ``work.genre``
    carries the work's genre tags from the closed TMDB genre set (canonical
    English names): "Action", "Adventure", "Animation", "Comedy", "Crime",
    "Documentary", "Drama", "Family", "Fantasy", "History", "Horror",
    "Music", "Mystery", "Romance", "Science Fiction", "TV Movie", "Thriller",
    "War", "Western", "Action & Adventure", "Kids", "News", "Reality",
    "Sci-Fi & Fantasy", "Soap", "Talk", "War & Politics".
    """
    n = (
        await db.execute(
            select(DownloadNotification)
            .where(DownloadNotification.id == notification_id)
            .options(
                selectinload(DownloadNotification.deliveries).selectinload(
                    WebhookDelivery.webhook
                )
            )
        )
    ).scalar_one_or_none()
    if n is None:
        return _error(404, "NOT_FOUND", "Notification not found")
    # Validate the stored snapshot against the payload contract; tolerate
    # legacy snapshots that predate schema keys (fall back to raw).
    try:
        payload = NotificationPayload.model_validate(n.payload).model_dump(
            exclude_unset=True
        )
    except Exception:
        payload = n.payload
    return success_response({
        **_list_item(n),
        "payload": payload,
        "deliveries": [_delivery_out(d) for d in n.deliveries],
    })


@router.post("/notifications/retry")
async def retry_notifications(
    body: RetryRequest, db: AsyncSession = Depends(get_db)
):
    """Bulk retry: reset matching deliveries to due-immediately.

    ``mode="failed"`` resets only failed deliveries; ``mode="all"`` resets
    every non-pending delivery. Optionally scoped by notification
    ``created_at >= since`` and/or ``agent_id``.
    """
    reset = await reset_deliveries_for_retry(
        db, body.mode, since=body.since, agent_id=body.agent_id
    )
    return success_response({"reset": reset})


@router.post("/notifications/{notification_id}/retry")
async def retry_notification(
    notification_id: str,
    body: NotificationRetryRequest,
    db: AsyncSession = Depends(get_db),
):
    """Manual UI retry for one notification: reset its deliveries to
    pending and make them due immediately."""
    n = await _get_notification_or_404(db, notification_id)
    if isinstance(n, JSONResponse):
        return n
    reset = await reset_deliveries_for_retry(
        db, body.mode, notification_id=notification_id
    )
    return success_response({"reset": reset})
