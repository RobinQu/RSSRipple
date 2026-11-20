"""External-dependency failure logs and their durable manual retry actions."""

from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_log import AppLog
from app.models.file_resource import FileResource
from app.utils.time import utcnow

ACTIVE_LOG_STATUSES = ("failed", "retrying")
LOG_RETENTION_DAYS = 7
LOG_CATEGORIES = ("metadata", "torrent", "magnet", "listing")


class RetryAlreadyRunningError(RuntimeError):
    pass


async def record_external_failure(
    db: AsyncSession,
    *,
    category: str,
    message: object,
    resource_id: str,
    channel_id: str | None,
    attempts: int,
) -> AppLog:
    if category not in LOG_CATEGORIES:
        raise ValueError(f"unsupported external log category: {category}")
    row = await db.scalar(
        select(AppLog)
        .where(
            AppLog.resource_id == resource_id,
            AppLog.category == category,
            AppLog.status.in_(ACTIVE_LOG_STATUSES),
        )
        .order_by(AppLog.updated_at.desc(), AppLog.id.desc())
        .limit(1)
    )
    text = (str(message) or "external dependency failed")[:4000]
    if row is None:
        row = AppLog(
            category=category,
            resource_id=resource_id,
            channel_id=channel_id,
        )
        db.add(row)
    row.message = text
    row.attempts = max(0, attempts)
    row.status = "failed"
    row.resolved_at = None
    row.updated_at = utcnow()
    await db.flush()
    return row


async def resolve_external_logs(
    db: AsyncSession,
    resource_id: str,
    categories: tuple[str, ...] | list[str] | None = None,
) -> int:
    predicates = [
        AppLog.resource_id == resource_id,
        AppLog.status.in_(ACTIVE_LOG_STATUSES),
    ]
    if categories:
        predicates.append(AppLog.category.in_(categories))
    result = await db.execute(
        update(AppLog)
        .where(*predicates)
        .values(status="resolved", resolved_at=utcnow(), updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


async def retry_external_failure(db: AsyncSession, log: AppLog) -> None:
    """Persist a fresh attempt generation before delivering its queue wakeup."""
    if log.status == "retrying":
        raise RetryAlreadyRunningError("retry already pending")
    if not log.resource_id:
        raise ValueError("log has no retryable resource")
    resource = await db.get(FileResource, log.resource_id)
    if resource is None:
        raise ValueError("resource not found")

    reparse_request = None
    if log.category == "metadata":
        from app.services.resource_reparse_requests import create_request

        resource.metadata_attempts = 0
        resource.metadata_failure_type = None
        resource.last_metadata_attempt_at = None
        reparse_request = await create_request(db, resource.id, resource.channel_id)
        if reparse_request is None:
            raise RetryAlreadyRunningError("metadata reparse already pending")
    elif log.category == "torrent":
        resource.torrent_fetch_attempts = 0
        resource.torrent_fetch_error = None
        resource.torrent_fetch_next_attempt_at = utcnow()
    elif log.category == "magnet":
        resource.magnet_resolve_attempt_id = None
        resource.magnet_resolve_attempts = 0
        resource.magnet_resolve_error = None
    elif log.category == "listing":
        resource.listing_reconcile_attempts = 0
        resource.listing_reconcile_error = None
        resource.listing_reconcile_next_attempt_at = utcnow()
    else:
        raise ValueError("log category is not retryable")

    log.status = "retrying"
    log.retry_count += 1
    log.last_retried_at = utcnow()
    log.updated_at = utcnow()
    await db.commit()

    if log.category == "metadata":
        from app.services.resource_reparse_requests import wake_request

        await wake_request(reparse_request)
    elif log.category == "torrent":
        from app.services.task_queue import task_queue

        await task_queue.enqueue(
            "fetch_torrent", f"torrent-fetch:{resource.id}", {"resource_id": resource.id}
        )
    elif log.category == "magnet":
        from app.services import magnet_resolve

        await magnet_resolve.enqueue_resolution(resource.id)
    elif log.category == "listing":
        from app.services.resource_listing import enqueue_listing_reconcile

        await enqueue_listing_reconcile(resource.id)


async def cleanup_app_logs(db: AsyncSession) -> int:
    from app.config import settings

    cutoff = utcnow() - timedelta(days=settings.app_log_retention_days)
    result = await db.execute(delete(AppLog).where(AppLog.updated_at < cutoff))
    return result.rowcount or 0
