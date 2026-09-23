"""Isolate snapshot generation failures from other tasks and delivery stages."""
import logging
import uuid
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database import committed_session, retry_on_lock
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.notification_build_failure import NotificationBuildFailure
from app.services.notify_service import create_notification_for_task
from app.services.task_queue import ExecutionOwnershipLostError
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


async def _attempt(task_id):
    from app.services.task_queue import require_execution_ownership

    async with committed_session() as db:
        task = await db.get(DownloadTask, task_id)
        if task is None or task.status != "completed":
            return None
        failure = await db.scalar(select(NotificationBuildFailure).where(
            NotificationBuildFailure.download_task_id == task_id,
        ))
        if failure is not None and failure.next_attempt_at > utcnow():
            return None
        notification, created = await create_notification_for_task(db, task)
        await require_execution_ownership()
        await db.execute(delete(NotificationBuildFailure).where(
            NotificationBuildFailure.download_task_id == task_id,
        ))
        return notification.id if created else None


async def _record_failure(task_id, message):
    from app.services.task_queue import require_execution_ownership

    await require_execution_ownership()
    async with committed_session() as db:
        task = await db.get(DownloadTask, task_id)
        if task is None or task.status != "completed":
            return
        if await db.scalar(select(DownloadNotification.id).where(
            DownloadNotification.download_task_id == task_id,
        )):
            return
        table = NotificationBuildFailure
        insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
        now = utcnow()
        statement = insert(table).values(
            id=str(uuid.uuid4()), download_task_id=task_id, attempt_count=1,
            next_attempt_at=now, updated_at=now, error_message=message[:2048],
        ).on_conflict_do_update(
            index_elements=["download_task_id"],
            set_={"attempt_count": table.attempt_count + 1, "updated_at": now,
                  "error_message": message[:2048]},
        ).returning(table.attempt_count)
        attempts = await db.scalar(statement)
        await require_execution_ownership()
        delay = min(1800, 30 * 2 ** min(attempts - 1, 6))
        await db.execute(update(table).where(table.download_task_id == task_id).values(
            next_attempt_at=now + timedelta(seconds=delay),
        ))


async def build_task_notifications(task_ids):
    """Commit each task independently; return only newly committed snapshot ids."""
    created = []
    for task_id in task_ids:
        try:
            notification_id = await retry_on_lock(lambda: _attempt(task_id))
            if notification_id is not None:
                created.append(notification_id)
        except ExecutionOwnershipLostError:
            raise
        except Exception as error:
            logger.warning("[notify] task %s snapshot generation failed: %s", task_id, error)
            message = str(error)
            try:
                await retry_on_lock(lambda: _record_failure(task_id, message))
            except ExecutionOwnershipLostError:
                raise
            except Exception:
                logger.exception("[notify] task %s failure state could not be saved", task_id)
    return created
