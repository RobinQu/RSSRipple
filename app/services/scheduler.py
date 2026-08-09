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
    from app.config import settings

    global _scheduler
    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled via SCHEDULER_ENABLED=false")
        return
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
    # FTS shadow-table reconciliation: heal any base/shadow divergence the
    # upsert/delete call sites miss (scripts, dedup merges, swallowed write
    # failures). Cheap full diff at current table sizes.
    _scheduler.add_job(
        _reconcile_fts,
        trigger=IntervalTrigger(minutes=5),
        id="fts_reconcile",
        replace_existing=True,
        next_run_time=utcnow() + timedelta(minutes=1),
    )
    # Download notifications: enqueue rows for freshly completed tasks, then
    # deliver every due pending notification to its Agent's webhook.
    _scheduler.add_job(
        _process_download_notifications,
        trigger=IntervalTrigger(minutes=1),
        id="download_notifications",
        replace_existing=True,
        next_run_time=utcnow() + timedelta(minutes=1),
    )
    _scheduler.start()
    logger.info("Scheduler started")


async def shutdown_scheduler() -> None:  # pragma: no cover - wiring only
    global _scheduler
    if _scheduler is None:
        return
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

    from app.config import settings
    from app.models.channel import Channel

    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled; skipping channel fetch job setup")
        return
    result = await db.execute(select(Channel).where(Channel.status != "inactive"))
    channels = result.scalars().all()
    for ch in channels:
        schedule_channel(ch)
    logger.info("Scheduled %d channel fetch jobs", len(channels))


async def setup_metadata_refresh_job(db) -> None:  # pragma: no cover - wiring only
    """Register the optional periodic works metadata refresh job from settings."""
    from app.config import settings

    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled; skipping metadata refresh job setup")
        return
    await reschedule_metadata_refresh_job(db)


async def reschedule_metadata_refresh_job(db) -> None:  # pragma: no cover - wiring only
    from app.config import settings
    from app.services.settings_service import (
        DEFAULT_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
        MAX_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
        MIN_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
        SETTING_METADATA_AUTO_REFRESH_ENABLED,
        SETTING_METADATA_AUTO_REFRESH_INTERVAL_MINUTES,
        get_bool_setting,
        get_int_setting,
    )

    if not settings.scheduler_enabled:
        return
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
    from app.config import settings

    if not settings.scheduler_enabled:
        return
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
    if _scheduler is None:
        return
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
    from sqlalchemy import and_, or_, select

    from app.clients.downloader import get_downloader_client
    from app.database import committed_session
    from app.models.download_task import DownloadTask
    from app.models.downloader import DownloaderInstance

    async with committed_session() as db:
        stmt = select(DownloadTask).where(
            or_(
                DownloadTask.status.in_(["pending", "queued", "downloading"]),
                # Self-heal: tasks flipped to error by a past outage cascade
                # (their error_message carries the "Transmission unreachable"
                # prefix) still have live torrents in the daemon. Re-include
                # them so tracking resumes once the downloader is back.
                # Tasks without a torrent id were never submitted successfully
                # and must go through retry instead.
                and_(
                    DownloadTask.status == "error",
                    DownloadTask.error_message.like("Transmission unreachable%"),
                    DownloadTask.transmission_torrent_id.isnot(None),
                ),
            )
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
                    # Clear a stale outage error once the torrent syncs again.
                    task.error_message = None
                    if torrent["is_finished"] or (
                        # leftUntilDone is also 0 before a magnet's metadata
                        # arrives - require a known size so fresh magnets are
                        # not mistaken for finished torrents.
                        torrent.get("left_until_done", 1) == 0
                        and torrent.get("total_size", 0) > 0
                    ):
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
            except Exception:
                # A failed RPC says nothing about the tasks themselves — the
                # torrents keep running in the daemon. Only flag the
                # downloader; task statuses stay as last known and resume
                # syncing on the next successful pass.
                downloader.status = "error"
                downloader.last_checked_at = utcnow()  # type: ignore[arg-type]
            # Commit per downloader: the next iteration's queries would
            # otherwise autoflush these pending UPDATEs right before its RPC,
            # holding the SQLite write lock for the whole list_torrents call.
            await db.commit()


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

        # Tasks with an unconsumed download notification must survive: the
        # notification's payload references the task, and ack removes the
        # torrent via it.
        from app.models.download_notification import DownloadNotification

        open_notifications = (
            select(DownloadNotification.download_task_id)
            .where(DownloadNotification.status != "done")
            .scalar_subquery()
        )

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
                DownloadTask.id.notin_(open_notifications),
            ))
            expired_tasks = (await db.execute(tasks_stmt)).scalars().all()
            for t in expired_tasks:
                await db.delete(t)
                deleted_count += 1

        # Retention for consumed notifications
        from app.config import settings

        notify_cutoff = now - timedelta(days=settings.notify_retention_days)
        old_notifications = (await db.execute(select(DownloadNotification).where(and_(
            DownloadNotification.status == "done",
            DownloadNotification.processed_at.isnot(None),
            DownloadNotification.processed_at < notify_cutoff,
        )))).scalars().all()
        for n in old_notifications:
            await db.delete(n)

        if stale:
            logger.info("Expired %d stale pending decisions", len(stale))
        if deleted_count:
            logger.info("Cleaned up %d expired completed tasks", deleted_count)
        if old_notifications:
            logger.info("Cleaned up %d consumed notifications", len(old_notifications))

        # Auto-cleanup of stale unresolved FileResources for channels that have
        # opted in (per-channel enable + age threshold).
        from app.services.resource_cleanup import cleanup_stale_unresolved_resources

        try:
            report = await cleanup_stale_unresolved_resources(db)
            if report["deleted"]:
                logger.info(
                    "Auto-cleaned %d unresolved resources on %d channels",
                    report["deleted"], report["channels"],
                )
        except Exception as e:
            logger.warning("Unresolved-resource cleanup failed: %s", e)


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


async def _process_download_notifications() -> None:
    """Per-minute: enqueue notifications for newly completed tasks, then
    deliver due pending ones (backoff retries included). Disabled unless
    NOTIFY_ENABLED=true so deployments without a consumer pay nothing."""
    from app.config import settings

    if not settings.notify_enabled:
        return
    from sqlalchemy import select

    from app.database import committed_session
    from app.models.download_notification import DownloadNotification
    from app.models.download_task import DownloadTask
    from app.services.notify_service import (
        create_notification_for_task,
        deliver_due_notifications,
    )

    async with committed_session() as db:
        try:
            notified = select(DownloadNotification.download_task_id).scalar_subquery()
            stmt = select(DownloadTask).where(
                DownloadTask.status == "completed",
                DownloadTask.id.notin_(notified),
            )
            tasks = (await db.execute(stmt)).scalars().all()
            enqueued = 0
            for task in tasks:
                _, was_created = await create_notification_for_task(db, task)
                if was_created:
                    enqueued += 1
                await db.commit()
            stats = await deliver_due_notifications(db)
            if enqueued or stats["delivered"] or stats["failed"]:
                logger.info(
                    "[notify] enqueued=%d delivered=%d failed=%d skipped=%d",
                    enqueued, stats["delivered"], stats["failed"], stats["skipped"],
                )
        except Exception as e:
            logger.warning("[notify] processing tick failed: %s", e)


async def _reconcile_fts() -> None:
    """Every 5 minutes: reconcile FTS shadow tables with the base tables."""
    from app.database import committed_session
    from app.services.fts import reconcile_fts

    try:
        async with committed_session() as db:
            report = await reconcile_fts(db)
        if report["updated"] or report["deleted"]:
            logger.info(
                "[fts] reconcile: %d rewritten, %d orphans removed",
                report["updated"], report["deleted"],
            )
    except Exception as e:
        logger.warning("[fts] reconcile job failed: %s", e)
