"""APScheduler integration for periodic jobs."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.utils.time import utcnow

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
        next_run_time=utcnow() + timedelta(seconds=30),
    )
    _scheduler.add_job(
        _cleanup_expired,
        trigger=CronTrigger(hour=3, minute=0),
        id="daily_cleanup",
        replace_existing=True,
    )
    _scheduler.add_job(
        _dedup_metadata,
        trigger=CronTrigger(hour=4, minute=0),
        id="daily_dedup",
        replace_existing=True,
    )
    _scheduler.add_job(
        _check_downloader_connections,
        trigger=IntervalTrigger(hours=1),
        id="check_downloaders",
        replace_existing=True,
        next_run_time=utcnow() + timedelta(minutes=2),
    )
    # Standalone metadata backfill: re-run retry-eligible unmatched resources
    # across all channels, independent of fetch_channel. Decouples metadata
    # repair from feed fetches so a slow/quiet feed can't starve it. The task
    # uses a stable key so the queue dedup gates it to run back-to-back
    # (continuous catch-up while unparsed resources remain).
    _scheduler.add_job(
        _run_metadata_backfill,
        trigger=IntervalTrigger(minutes=5),
        id="metadata_backfill",
        replace_existing=True,
        next_run_time=utcnow() + timedelta(seconds=30),
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
    """Register interval jobs for all non-inactive channels at startup.

    Channels in the ``error`` state (a previous fetch failed) are still
    scheduled so they retry and recover when the feed becomes reachable again;
    only ``inactive`` (paused) channels are skipped.
    """
    from sqlalchemy import select

    from app.models.channel import Channel
    result = await db.execute(select(Channel).where(Channel.status != "inactive"))
    channels = result.scalars().all()
    for ch in channels:
        schedule_channel(ch)
    logger.info("Scheduled %d channel fetch jobs", len(channels))


async def setup_metadata_refresh_job(db) -> None:  # pragma: no cover - wiring only
    """Register the optional periodic works metadata refresh job from settings."""
    await reschedule_metadata_refresh_job(db)


async def reschedule_metadata_refresh_job(db) -> None:  # pragma: no cover - wiring only
    from app.services.settings_service import (
        DEFAULT_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
        MAX_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
        MIN_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
        SETTING_METADATA_AUTO_REFRESH_ENABLED,
        SETTING_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
        get_bool_setting,
        get_int_setting,
    )

    sched = get_scheduler()
    job_id = "metadata_refresh"
    try:
        sched.remove_job(job_id)
    except Exception:
        pass

    enabled = await get_bool_setting(db, SETTING_METADATA_AUTO_REFRESH_ENABLED, False)
    if not enabled:
        logger.info("Periodic metadata refresh is disabled")
        return

    interval_minutes = await get_int_setting(
        db,
        SETTING_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
        DEFAULT_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
        MIN_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
        MAX_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
    )
    sched.add_job(
        _run_metadata_refresh,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id=job_id,
        replace_existing=True,
        next_run_time=utcnow() + timedelta(seconds=5),
    )
    logger.info("Scheduled periodic metadata refresh every %d minutes", interval_minutes)


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
        next_run_time=utcnow() + timedelta(seconds=5),
    )


def unschedule_channel(channel_id: str) -> None:  # pragma: no cover - wiring only
    sched = get_scheduler()
    job_id = f"channel:{channel_id}"
    try:
        sched.remove_job(job_id)
    except Exception:
        pass


def reschedule_channel(channel: Any) -> None:  # pragma: no cover - wiring only
    # Re-schedule for any non-inactive channel so edits (fetch_interval,
    # metadata_source, ...) take effect even when the channel is in an error
    # state - the next fetch re-evaluates and flips status back to active on
    # success. Only paused (inactive) channels stay unscheduled.
    unschedule_channel(channel.id)
    if channel.status != "inactive":
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


async def _run_metadata_backfill() -> None:  # pragma: no cover - wiring only
    """Enqueue the standalone global metadata backfill.

    Uses a stable key (``backfill:metadata``) so the task-queue dedup gates
    consecutive ticks to run back-to-back: while a backfill is in flight the
    next 5-min tick is dropped, and as soon as it finishes the next tick
    enqueues again. This yields continuous catch-up on unparsed resources
    without overlapping runs.
    """
    from app.services.task_queue import task_queue

    try:
        await task_queue.enqueue(
            "backfill_metadata",
            "backfill:metadata",
            {},
        )
    except Exception as e:
        logger.warning("Failed to enqueue metadata backfill: %s", e)


async def _run_metadata_refresh() -> None:  # pragma: no cover - wiring only
    from uuid import uuid4

    from sqlalchemy import select

    from app.database import committed_session
    from app.models.movie import Movie
    from app.models.series import TVSeries
    from app.services.settings_service import resolve_default_metadata_source
    from app.services.task_queue import task_queue

    async with committed_session() as db:
        try:
            source = await resolve_default_metadata_source(db)
        except ValueError as e:
            logger.warning("Periodic metadata refresh skipped: %s", e)
            return

        series_ids = (await db.execute(select(TVSeries.id))).scalars().all()
        movie_ids = (await db.execute(select(Movie.id))).scalars().all()
        items = (
            [{"id": wid, "content_type": "tv"} for wid in series_ids]
            + [{"id": wid, "content_type": "movie"} for wid in movie_ids]
        )

    if not items:
        return

    await task_queue.enqueue(
        "refresh_works_metadata",
        f"periodic_refresh_works:{uuid4().hex}",
        {"items": items, "source": source},
    )


async def _sync_download_progress() -> None:
    from sqlalchemy import select

    from app.clients.downloader import get_downloader_client
    from app.database import committed_session
    from app.models.download_task import DownloadTask
    from app.models.downloader import DownloaderInstance

    async with committed_session() as db:
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
                wrapper = get_downloader_client(downloader)
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
                        task.completed_at = utcnow()
                    elif torrent["status"] == "stopped":
                        task.status = "paused"
                    elif torrent["status"] in ("downloading", "download pending", "queued"):
                        task.status = "downloading" if torrent["rate_download"] > 0 else "queued"
                    else:
                        task.status = "downloading"
                downloader.status = "connected"
                downloader.last_checked_at = utcnow()
            except Exception as e:
                downloader.status = "error"
                downloader.last_checked_at = utcnow()  # type: ignore[arg-type]
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


async def _cleanup_expired() -> None:
    """Daily job: expire pending decisions and delete completed tasks older than agent.task_expire_days."""
    from sqlalchemy import and_, select

    from app.database import committed_session
    from app.models.agent import Agent
    from app.models.download_task import DownloadTask
    from app.models.pending_decision import PendingDecision

    async with committed_session() as db:
        now = utcnow()
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

        if stale:
            logger.info("Expired %d stale pending decisions", len(stale))
        if deleted_count:
            logger.info("Cleaned up %d expired completed tasks", deleted_count)


async def _check_downloader_connections() -> None:
    """Hourly connectivity check for all downloaders."""
    from sqlalchemy import select

    from app.clients.downloader import get_downloader_client
    from app.database import committed_session
    from app.models.downloader import DownloaderInstance

    async with committed_session() as db:
        result = await db.execute(select(DownloaderInstance))
        downloaders = result.scalars().all()
        for dl in downloaders:
            try:
                wrapper = get_downloader_client(dl)
                ok, _msg = await wrapper.test_connection()
                dl.status = "connected" if ok else "error"
            except Exception:
                dl.status = "error"
            dl.last_checked_at = utcnow()


async def _dedup_metadata() -> None:
    """Daily: merge duplicate TVSeries/Movie rows.

    Safety net for the metadata agents occasionally creating a second row for
    an already-known work (e.g. when a channel's LLM matches via a different
    external source). Clustering keys on shared titles + aliases, so this only
    collapses rows that are provably the same work. Idempotent.
    """
    from app.database import committed_session
    from app.services.metadata_dedup import merge_duplicate_metadata

    async with committed_session() as db:
        try:
            report = await merge_duplicate_metadata(db)
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.warning("Metadata dedup failed: %s", e)
            return
    if report.series_removed or report.movies_removed:
        logger.info(
            "Metadata dedup: removed %d series, %d movie duplicates",
            report.series_removed,
            report.movies_removed,
        )
