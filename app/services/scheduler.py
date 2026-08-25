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

# 每 tick 补扫的无计划通知上限（organize 兜底重试）。
_ORGANIZE_ORPHAN_BATCH = 50


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
    # All periodic jobs are queue-only: each tick enqueues a job with a stable
    # key (``job:<type>``) instead of running the function body in the
    # scheduler process. The queue's active-key dedup collapses concurrent
    # ticks (including duplicate schedulers across multiple workers) to a
    # single queued job, and the queue consumer executes it exactly once. The
    # function bodies below stay directly callable (unit tests invoke them).
    _scheduler.add_job(
        _enqueue_sync_progress,
        trigger=IntervalTrigger(minutes=1),
        id="sync_progress",
        replace_existing=True,
        next_run_time=utcnow() + timedelta(seconds=30),
    )
    # Daily jobs get a wide misfire grace window: APScheduler's default grace
    # is 1 second, and with several workers running LLM-heavy metadata work
    # the event loop is routinely blocked past the exact fire moment — the
    # daily run was then skipped outright (next chance 24h later). One hour
    # of grace lets a blocked scheduler catch up as soon as its loop frees.
    _scheduler.add_job(
        _enqueue_daily_cleanup,
        trigger=CronTrigger(hour=3, minute=0),
        id="daily_cleanup",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.add_job(
        _enqueue_daily_dedup,
        trigger=CronTrigger(hour=4, minute=0),
        id="daily_dedup",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.add_job(
        _enqueue_check_downloaders,
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
    # FTS sidecar synchronization (Turso only — on PostgreSQL there is no
    # sidecar; ``search_text`` + pg_trgm is maintained in-transaction by the
    # ORM flush hooks): replay fts_outbox change rows onto the sidecar shadow
    # tables every 30s (outbox rows are enqueued atomically with the base-row
    # transaction by the ORM before_flush hook). The hourly reconcile below
    # heals whatever this misses (raw SQL, scripts, swallowed write failures).
    from app.database import is_turso_url

    if is_turso_url(settings.database_url):
        _scheduler.add_job(
            _enqueue_fts_drain,
            trigger=IntervalTrigger(seconds=30),
            id="fts_drain",
            replace_existing=True,
            next_run_time=utcnow() + timedelta(seconds=5),
        )
        # FTS shadow-table reconcile: full diff base vs shadow as a backstop
        # for paths that bypass the outbox (scripts, dedup merges, direct SQL).
        _scheduler.add_job(
            _enqueue_fts_reconcile,
            trigger=IntervalTrigger(hours=1),
            id="fts_reconcile",
            replace_existing=True,
            next_run_time=utcnow() + timedelta(minutes=1),
        )
    # Download notifications: enqueue rows for freshly completed tasks, fan
    # out to per-webhook deliveries, then deliver every due pending one.
    _scheduler.add_job(
        _enqueue_download_notifications,
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


def schedule_channel(channel: Any) -> None:  # pragma: no cover - wiring only
    from app.config import settings
    from app.services.settings_service import DEFAULT_METADATA_REFRESH_INTERVAL_MINUTES

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
    # Per-channel periodic work-metadata refresh (optional, off by default).
    # Same queue-only pattern as the fetch job: a stable enqueue key plus the
    # queue's active-key dedup collapses duplicate scheduler ticks across
    # workers to a single execution.
    if getattr(channel, "metadata_refresh_enabled", False):
        minutes = channel.metadata_refresh_interval_minutes or (
            DEFAULT_METADATA_REFRESH_INTERVAL_MINUTES
        )
        sched.add_job(
            _run_channel_works_refresh,
            trigger=IntervalTrigger(minutes=minutes),
            id=f"channel-refresh:{channel.id}",
            args=[channel.id],
            replace_existing=True,
            next_run_time=utcnow() + timedelta(seconds=5),
        )


def unschedule_channel(channel_id: str) -> None:  # pragma: no cover - wiring only
    if _scheduler is None:
        return
    sched = get_scheduler()
    for job_id in (f"channel:{channel_id}", f"channel-refresh:{channel_id}"):
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


# ---------------------------------------------------------------------------
# Periodic-job enqueue wrappers
#
# The function bodies further below stay directly callable (unit tests invoke
# them after monkeypatching the session factory). The scheduler only registers
# these thin wrappers. Two layers keep a job to one execution per interval
# across N worker processes, each running its own scheduler:
#
# 1. ``throttle`` — a cross-process tick key (SET NX EX, TTL = the interval)
#    so only the first tick of each interval proceeds to enqueue at all;
# 2. the queue's active-key dedup — collapses any remaining concurrent
#    duplicates to a single queued job, executed exactly once by whichever
#    consumer pops it.
# ---------------------------------------------------------------------------

# Throttle TTL per job type: slightly below the tick interval so a slow tick
# never eats the next interval's slot.
_PERIODIC_THROTTLE_TTL = {
    "sync_progress": 50,
    "download_notifications": 50,
    "fts_drain": 25,
    "fts_reconcile": 3540,
    "check_downloaders": 3540,
    "daily_cleanup": 86340,
    "daily_dedup": 86340,
}


async def _enqueue_periodic_job(job_type: str) -> None:
    from app.services.task_queue import task_queue

    try:
        if not await task_queue.throttle(
            job_type, _PERIODIC_THROTTLE_TTL[job_type]
        ):
            return  # another worker's scheduler already ticked this interval
        await task_queue.enqueue(job_type, f"job:{job_type}", {})
    except Exception as e:
        logger.warning("Failed to enqueue %s: %s", job_type, e)


async def _enqueue_sync_progress() -> None:
    await _enqueue_periodic_job("sync_progress")


async def _enqueue_daily_cleanup() -> None:
    await _enqueue_periodic_job("daily_cleanup")


async def _enqueue_daily_dedup() -> None:
    await _enqueue_periodic_job("daily_dedup")


async def _enqueue_check_downloaders() -> None:
    await _enqueue_periodic_job("check_downloaders")


async def _enqueue_fts_drain() -> None:
    await _enqueue_periodic_job("fts_drain")


async def _enqueue_fts_reconcile() -> None:
    await _enqueue_periodic_job("fts_reconcile")


async def _enqueue_download_notifications() -> None:
    await _enqueue_periodic_job("download_notifications")


async def _run_channel_works_refresh(channel_id: str) -> None:  # pragma: no cover - wiring only
    """Enqueue the per-channel periodic work-metadata refresh.

    Stable key ``channel-refresh:<id>``: while a run is queued/active the next
    tick's enqueue is deduped by the queue's active-key check, so overlapping
    schedulers across workers cannot pile up concurrent runs.
    """
    from app.services.task_queue import task_queue

    try:
        await task_queue.enqueue(
            "refresh_channel_works",
            f"channel-refresh:{channel_id}",
            {"channel_id": channel_id},
        )
    except Exception as e:
        logger.warning(
            "Failed to enqueue works refresh for channel %s: %s", channel_id, e
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

        # Tasks whose notification has any undelivered (non-done) webhook
        # delivery must survive: the delivery retry loop still needs the
        # task row the notification's payload references.
        from app.models.download_notification import DownloadNotification
        from app.models.webhook_delivery import WebhookDelivery

        open_notifications = (
            select(DownloadNotification.download_task_id)
            .join(
                WebhookDelivery,
                WebhookDelivery.notification_id == DownloadNotification.id,
            )
            .where(WebhookDelivery.status != "done")
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

        # Retention for old notifications (deliveries cascade via ORM delete)
        from app.config import settings

        notify_cutoff = now - timedelta(days=settings.notify_retention_days)
        old_notifications = (await db.execute(select(DownloadNotification).where(
            DownloadNotification.created_at < notify_cutoff,
        ))).scalars().all()
        for n in old_notifications:
            await db.delete(n)

        if stale:
            logger.info("Expired %d stale pending decisions", len(stale))
        if deleted_count:
            logger.info("Cleaned up %d expired completed tasks", deleted_count)
        if old_notifications:
            logger.info("Cleaned up %d expired notifications", len(old_notifications))

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
    """Per-minute: enqueue notifications for newly completed tasks, fan them
    out to per-webhook deliveries, then deliver due pending ones (backoff
    retries included). Disabled unless NOTIFY_ENABLED=true so deployments
    without a consumer pay nothing."""
    from app.config import settings

    if not settings.notify_enabled:
        return
    from sqlalchemy import select

    from app.database import committed_session
    from app.models.agent_webhook import AgentWebhook
    from app.models.download_notification import DownloadNotification
    from app.models.download_task import DownloadTask
    from app.models.organize_plan import OrganizePlan
    from app.models.organize_rule import OrganizeRule
    from app.services.notify_service import (
        create_notification_for_task,
        deliver_due_deliveries,
        ensure_deliveries,
    )

    async with committed_session() as db:
        try:
            notified = select(DownloadNotification.download_task_id).scalar_subquery()
            # 未注册启用 webhook 的 agent 不生成通知，避免堆积无用记录；但内置
            # organize 也是通知快照的消费者（常开，存在启用规则即激活），因此
            # 有启用 organize 规则时同样生成通知以驱动变更计划。
            has_webhook = (
                select(AgentWebhook.id)
                .where(
                    AgentWebhook.agent_id == DownloadTask.agent_id,
                    AgentWebhook.enabled.is_(True),
                )
                .exists()
            )
            organize_active = (
                select(OrganizeRule.id)
                .where(OrganizeRule.enabled.is_(True))
                .exists()
            )
            stmt = select(DownloadTask).where(
                DownloadTask.status == "completed",
                DownloadTask.id.notin_(notified),
                has_webhook | organize_active,
            )
            tasks = (await db.execute(stmt)).scalars().all()
            enqueued = 0
            created_notifications = []
            for task in tasks:
                notification, was_created = await create_notification_for_task(db, task)
                if was_created:
                    enqueued += 1
                    if notification is not None:
                        created_notifications.append(notification)
                await db.commit()
            # organize 规划步：补建通知之后、fan-out 之前；失败不中断 tick 其余阶段
            plan_targets = list(created_notifications)
            try:
                # 兜底补扫：没有任何计划行的存量通知（规划被意外异常中断，
                # 或旧版本拒绝未落计划），每 tick 限量重试。确定性拒绝
                # （PlanError）已落 failed 计划行，不在此列，不会每 tick
                # 重复规划。
                plan_exists = (
                    select(OrganizePlan.id)
                    .where(OrganizePlan.notification_id == DownloadNotification.id)
                    .exists()
                )
                orphans = (
                    await db.execute(
                        select(DownloadNotification)
                        .where(~plan_exists)
                        .order_by(DownloadNotification.created_at.asc())
                        .limit(_ORGANIZE_ORPHAN_BATCH)
                    )
                ).scalars().all()
                seen = {n.id for n in plan_targets}
                plan_targets += [n for n in orphans if n.id not in seen]
            except Exception as e:
                logger.warning("[organize] orphan notification scan failed: %s", e)
            if plan_targets:
                try:
                    from app.services.organize_service import plan_for_notifications

                    ostats = await plan_for_notifications(db, plan_targets)
                    if any(ostats.values()):
                        logger.info(
                            "[organize] planned=%d rebuilt=%d uncategorized=%d "
                            "skipped=%d failed=%d",
                            ostats["planned"], ostats["rebuilt"],
                            ostats["uncategorized"], ostats["skipped"],
                            ostats["failed"],
                        )
                except Exception as e:
                    logger.warning("[organize] planning step failed: %s", e)
            fanned = await ensure_deliveries(db)
            stats = await deliver_due_deliveries(db)
            if enqueued or fanned or stats["delivered"] or stats["failed"]:
                logger.info(
                    "[notify] enqueued=%d fanned=%d delivered=%d failed=%d skipped=%d",
                    enqueued, fanned,
                    stats["delivered"], stats["failed"], stats["skipped"],
                )
        except Exception as e:
            logger.warning("[notify] processing tick failed: %s", e)


async def _drain_fts_outbox() -> None:
    """Every 30s: replay fts_outbox change rows onto the FTS sidecar."""
    from app.database import committed_session
    from app.services.fts import drain_fts_outbox

    try:
        async with committed_session() as db:
            n = await drain_fts_outbox(db)
        if n:
            logger.info("[fts] drained %d outbox rows", n)
    except Exception as e:
        logger.warning("[fts] drain job failed: %s", e)


async def _reconcile_fts() -> None:
    """Hourly: reconcile FTS shadow tables with the base tables."""
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
