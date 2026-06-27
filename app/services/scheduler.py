"""APScheduler integration for periodic jobs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialized")
    return _scheduler


async def init_scheduler() -> None:  # pragma: no cover - wiring only
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _sync_download_progress,
        trigger=IntervalTrigger(minutes=1),
        id="sync_progress",
        replace_existing=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=30),
    )
    _scheduler.add_job(
        _cleanup_expired,
        trigger=CronTrigger(hour=3, minute=0),
        id="daily_cleanup",
        replace_existing=True,
    )
    _scheduler.add_job(
        _check_downloader_connections,
        trigger=IntervalTrigger(hours=1),
        id="check_downloaders",
        replace_existing=True,
        next_run_time=datetime.now(UTC) + timedelta(minutes=2),
    )
    _scheduler.start()
    logger.info("Scheduler started")


async def shutdown_scheduler() -> None:  # pragma: no cover - wiring only
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler shut down")


async def setup_channel_jobs(db) -> None:  # pragma: no cover - wiring only
    """Register interval jobs for all active channels at startup."""
    from app.models.channel import Channel
    from sqlalchemy import select
    result = await db.execute(select(Channel).where(Channel.status == "active"))
    channels = result.scalars().all()
    for ch in channels:
        schedule_channel(ch)
    logger.info("Scheduled %d active channel fetch jobs", len(channels))


def schedule_channel(channel: Any) -> None:  # pragma: no cover - wiring only
    sched = get_scheduler()
    job_id = f"channel:{channel.id}"
    trigger = IntervalTrigger(seconds=channel.fetch_interval)
    sched.add_job(
        _run_channel_fetch,
        trigger=trigger,
        id=job_id,
        args=[channel.id],
        replace_existing=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=5),
    )


def unschedule_channel(channel_id: str) -> None:  # pragma: no cover - wiring only
    sched = get_scheduler()
    job_id = f"channel:{channel_id}"
    try:
        sched.remove_job(job_id)
    except Exception:
        pass


def reschedule_channel(channel: Any) -> None:  # pragma: no cover - wiring only
    unschedule_channel(channel.id)
    if channel.status == "active":
        schedule_channel(channel)


async def _run_channel_fetch(channel_id: str) -> None:  # pragma: no cover - wiring only
    from app.services.task_queue import task_queue
    try:
        await task_queue.enqueue(
            "fetch_channel",
            f"channel:{channel_id}",
            {"channel_id": channel_id},
        )
    except Exception as e:
        logger.warning("Failed to enqueue fetch for channel %s: %s", channel_id, e)


async def _sync_download_progress() -> None:
    from app.database import async_session_factory
    from app.models.download_task import DownloadTask
    from app.models.downloader import DownloaderInstance
    from app.clients.transmission import TransmissionWrapper
    from sqlalchemy import select

    async with async_session_factory() as db:
        try:
            stmt = select(DownloadTask).where(
                DownloadTask.status.in_(["pending", "queued", "downloading"])
            )
            tasks = (await db.execute(stmt)).scalars().all()
            by_downloader: dict[str, list[DownloadTask]] = {}
            for t in tasks:
                if not t.downloader_id:
                    continue
                by_downloader.setdefault(t.downloader_id, []).append(t)

            for dl_id, dl_tasks in by_downloader.items():
                downloader = await db.get(DownloaderInstance, dl_id)
                if not downloader:
                    continue
                try:
                    wrapper = TransmissionWrapper(
                        url=downloader.url,
                        username=downloader.username,
                        password=downloader.password,
                    )
                    torrents = await wrapper.list_torrents()
                    tmap = {t["id"]: t for t in torrents}
                    for task in dl_tasks:
                        torrent = tmap.get(task.transmission_torrent_id)
                        if torrent is None:
                            task.status = "cancelled"
                            continue
                        task.progress = torrent["percent_done"]
                        task.download_speed = torrent["rate_download"]
                        task.upload_speed = torrent["rate_upload"]
                        task.eta = torrent.get("eta_seconds")
                        if torrent["is_finished"] or torrent.get("left_until_done", 1) == 0:
                            task.status = "completed"
                            task.completed_at = datetime.now(UTC)
                        elif torrent["status"] == "stopped":
                            task.status = "paused"
                        elif torrent["status"] in ("downloading", "download pending", "queued"):
                            task.status = "downloading" if torrent["rate_download"] > 0 else "queued"
                        else:
                            task.status = "downloading"
                    downloader.status = "connected"
                    downloader.last_checked_at = datetime.now(UTC)
                except Exception as e:
                    downloader.status = "error"
                    downloader.last_checked_at = datetime.now(UTC)  # type: ignore[arg-type]
                    for task in dl_tasks:
                        # Don't override tasks that are already complete/cancelled
                        if task.status in ("pending", "queued", "downloading"):
                            task.status = "error"
                            task.error_message = f"Transmission unreachable: {e}"[:2000]
                    for task in dl_tasks:
                        # Don't override tasks that are already complete/cancelled
                        if task.status in ("pending", "queued", "downloading"):
                            task.status = "error"
                            task.error_message = f"Transmission unreachable: {e}"[:2000]
            await db.commit()
        except Exception as e:
            logger.exception("Progress sync failed: %s", e)


async def _cleanup_expired() -> None:
    """Daily job: expire pending decisions and delete completed tasks older than agent.task_expire_days."""
    from app.database import async_session_factory
    from app.models.pending_decision import PendingDecision
    from app.models.download_task import DownloadTask
    from app.models.agent import Agent
    from sqlalchemy import select, and_

    async with async_session_factory() as db:
        try:
            now = datetime.now(UTC)
            # Expire pending decisions past expires_at
            stale_stmt = select(PendingDecision).where(and_(
                PendingDecision.status == "pending",
                PendingDecision.expires_at.isnot(None),
                PendingDecision.expires_at < now,
            ))
            stale = (await db.execute(stale_stmt)).scalars().all()
            for d in stale:
                d.status = "expired"

            # Cleanup expired completed tasks per agent's task_expire_days
            agents_result = await db.execute(select(Agent))
            agents = agents_result.scalars().all()
            deleted_count = 0
            for agent in agents:
                expire_days = agent.task_expire_days or 30
                cutoff = now - timedelta(days=expire_days)
                tasks_stmt = select(DownloadTask).where(and_(
                    DownloadTask.agent_id == agent.id,
                    DownloadTask.status == "completed",
                    DownloadTask.completed_at.isnot(None),
                    DownloadTask.completed_at < cutoff,
                ))
                expired_tasks = (await db.execute(tasks_stmt)).scalars().all()
                for t in expired_tasks:
                    await db.delete(t)
                    deleted_count += 1

            await db.commit()
            if stale:
                logger.info("Expired %d stale pending decisions", len(stale))
            if deleted_count:
                logger.info("Cleaned up %d expired completed tasks", deleted_count)
        except Exception as e:
            logger.exception("Daily cleanup failed: %s", e)


async def _check_downloader_connections() -> None:
    """Hourly connectivity check for all downloaders."""
    from app.database import async_session_factory
    from app.models.downloader import DownloaderInstance
    from app.clients.transmission import TransmissionWrapper
    from sqlalchemy import select

    async with async_session_factory() as db:
        try:
            result = await db.execute(select(DownloaderInstance))
            downloaders = result.scalars().all()
            for dl in downloaders:
                try:
                    wrapper = TransmissionWrapper(url=dl.url, username=dl.username, password=dl.password)
                    ok, _msg = await wrapper.test_connection()
                    dl.status = "connected" if ok else "error"
                except Exception:
                    dl.status = "error"
                dl.last_checked_at = datetime.now(UTC)
            await db.commit()
        except Exception as e:
            logger.exception("Downloader connection check failed: %s", e)
