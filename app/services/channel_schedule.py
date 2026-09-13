"""Reconcile each process's channel jobs from committed database settings.

This is local scheduler maintenance, not a shared-queue job. Every worker
must run it; a global lease would leave the other workers' jobs stale.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import Channel
from app.services.settings_service import DEFAULT_METADATA_REFRESH_INTERVAL_MINUTES
from app.utils.time import utcnow

logger = logging.getLogger(__name__)
CHANNEL_RECONCILE_SECONDS = 30


async def reconcile_channel_jobs(
    db: AsyncSession,
    scheduler: AsyncIOScheduler,
    *,
    fetch_callback: Callable,
    refresh_callback: Callable,
) -> dict[str, int]:
    """Apply only changed jobs. A failed DB read leaves every job untouched."""
    rows = (await db.execute(select(
        Channel.id, Channel.status, Channel.fetch_interval,
        Channel.metadata_refresh_enabled, Channel.metadata_refresh_interval_minutes,
    ))).all()
    stats = dict(added=0, updated=0, removed=0, errors=0)
    retained: set[str] = set()
    for row in rows:
        fetch_id, refresh_id = f"channel:{row.id}", f"channel-refresh:{row.id}"
        if row.status == "inactive":
            continue
        try:
            fetch_seconds = row.fetch_interval
            refresh_seconds = (row.metadata_refresh_interval_minutes or DEFAULT_METADATA_REFRESH_INTERVAL_MINUTES) * 60
            if fetch_seconds <= 0 or (row.metadata_refresh_enabled and refresh_seconds <= 0):
                raise ValueError("channel interval must be positive")
            desired = [(fetch_id, fetch_seconds, fetch_callback)]
            if row.metadata_refresh_enabled:
                desired.append((refresh_id, refresh_seconds, refresh_callback))
            for job_id, seconds, callback in desired:
                retained.add(job_id)
                job = scheduler.get_job(job_id)
                if job is not None and isinstance(job.trigger, IntervalTrigger) and (
                    job.trigger.interval == timedelta(seconds=seconds)
                    and job.func == callback and tuple(job.args) == (row.id,)
                ):
                    continue  # do not postpone the next fetch on every reconcile
                scheduler.add_job(
                    callback, trigger=IntervalTrigger(seconds=seconds),
                    id=job_id, args=[row.id], replace_existing=True,
                    next_run_time=utcnow() + timedelta(seconds=5),
                )
                stats["updated" if job else "added"] += 1
        except Exception:
            # Preserve this channel's existing jobs; other channels still heal.
            retained.update((fetch_id, refresh_id))
            stats["errors"] += 1
            logger.exception("[scheduler] cannot reconcile channel %s", row.id)
    for job in scheduler.get_jobs():
        if job.id.startswith(("channel:", "channel-refresh:")) and job.id not in retained:
            try:
                scheduler.remove_job(job.id)
                stats["removed"] += 1
            except Exception:
                stats["errors"] += 1
                logger.exception("[scheduler] cannot remove channel job %s", job.id)
    if any(stats.values()):
        logger.info("[scheduler] channel reconciliation %s", stats)
    return stats
