"""Remove aged orphan reservations only after their logical job is retired."""

import logging
from datetime import timedelta

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.models.download_dispatch import DownloadDispatch
from app.models.download_task import DownloadTask
from app.utils.time import utcnow

logger = logging.getLogger(__name__)
RETENTION_DAYS = 7
PAGE_SIZE = 500


def _task_missing():
    return ~exists(select(DownloadTask.id).where(DownloadTask.id == DownloadDispatch.task_id))


async def cleanup_dispatch_reservations(engine, queue):
    if isinstance(engine, AsyncConnection):
        engine = engine.engine
    cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
    last = None
    deleted = 0
    while True:
        conditions = [
            DownloadDispatch.created_at < cutoff,
            DownloadDispatch.job_key.is_not(None),
            DownloadDispatch.job_id.is_not(None),
            _task_missing(),
        ]
        if last is not None:
            conditions.append(or_(
                DownloadDispatch.created_at > last[0],
                and_(DownloadDispatch.created_at == last[0], DownloadDispatch.id > last[1]),
            ))
        async with AsyncSession(engine) as db:
            rows = (await db.execute(select(
                DownloadDispatch.id, DownloadDispatch.job_key, DownloadDispatch.job_id,
                DownloadDispatch.created_at, DownloadDispatch.settled,
            ).where(*conditions).order_by(
                DownloadDispatch.created_at, DownloadDispatch.id,
            ).limit(PAGE_SIZE))).all()
        if not rows:
            return deleted
        for row in rows:
            try:
                retired = await queue.job_is_retired(row.job_key, row.job_id)
            except Exception:
                logger.warning("Reservation cleanup deferred: queue state unavailable", exc_info=True)
                return deleted
            if not retired:
                continue
            async with AsyncSession(engine) as db, db.begin():
                result = await db.execute(delete(DownloadDispatch).where(
                    DownloadDispatch.id == row.id,
                    DownloadDispatch.settled == row.settled,
                    _task_missing(),
                ))
                deleted += result.rowcount
        last = (rows[-1].created_at, rows[-1].id)
