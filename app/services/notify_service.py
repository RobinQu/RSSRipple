"""Download notification service.

Builds the per-task notification snapshot and delivers it to the Agent's
registered webhook with exponential backoff. RSSRipple's responsibility ends
at "persist the notification and deliver it" — all file planning/execution
lives in the external consumer (vault-organizer). See
docs/design/notifications.md.

Delivery has a single code path: the scheduler's per-minute tick calls
:func:`deliver_due_notifications`, which picks up every due ``pending`` row —
fresh ones (``next_attempt_at`` set to now at creation), backoff retries, and
manual UI retries (which reset the row to due-now). Nothing else sends
webhooks.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.agent import Agent
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 1800  # 30 min cap on the exponential backoff
WEBHOOK_TIMEOUT_SECONDS = 5.0
_DELIVERY_BATCH = 50  # max notifications delivered per scheduler tick
_BACKFILL_COMMIT_EVERY = 20


def backoff_delay(attempt_count: int) -> timedelta:
    """Exponential backoff after ``attempt_count`` failed deliveries."""
    seconds = settings.notify_retry_base_seconds * (2 ** max(0, attempt_count))
    return timedelta(seconds=min(seconds, MAX_BACKOFF_SECONDS))


def build_payload(
    notification_id: str,
    agent: Agent | None,
    task: DownloadTask,
    resource: FileResource | None,
    torrent_info: dict | None,
) -> dict:
    """Pure snapshot builder — everything the consumer needs, frozen at
    creation time. Later metadata changes do not affect this notification."""
    work: dict = {"type": None}
    if resource is not None and resource.series is not None:
        series: TVSeries = resource.series
        work = {
            "type": "series",
            "series_id": series.id,
            "title_en": series.title_en,
            "title_cn": series.title_cn,
            "original_title": series.original_title,
            "year": series.start_date.year if series.start_date else None,
            "content_type": series.content_type,
            "collection": (
                series.collection.display_name if series.collection else None
            ),
            "seasons": series.seasons,
            "episodes": [
                {"season": ep.season, "episode": ep.episode, "title": ep.title}
                for ep in series.episodes
            ],
        }
    elif resource is not None and resource.movie is not None:
        movie: Movie = resource.movie
        work = {
            "type": "movie",
            "movie_id": movie.id,
            "title_en": movie.title_en,
            "title_cn": movie.title_cn,
            "original_title": movie.original_title,
            "year": movie.release_date.year if movie.release_date else None,
            "content_type": movie.content_type,
            "collection": (
                movie.collection.display_name if movie.collection else None
            ),
            "seasons": None,
            "episodes": None,
        }

    payload = {
        "notification_id": notification_id,
        "agent": {"id": agent.id, "name": agent.name} if agent else None,
        "task": {
            "download_task_id": task.id,
            "download_dir": task.download_dir,
            "torrent_name": (torrent_info or {}).get("name"),
        },
        "resource": {
            "title_raw": resource.title_raw if resource else None,
            "season": resource.season if resource else None,
            "episode": resource.episode if resource else None,
            "is_batch": resource.is_batch if resource else False,
            "episode_start": resource.episode_start if resource else None,
            "episode_end": resource.episode_end if resource else None,
            "subtitle_langs": resource.subtitle_langs if resource else None,
            "resolution": resource.resolution if resource else None,
            "container": resource.container if resource else None,
            "title_year": resource.title_year if resource else None,
        },
        "work": work,
    }
    if torrent_info is not None:
        payload["files"] = torrent_info.get("files")
    return payload


async def _load_resource(db, file_resource_id: str) -> FileResource | None:
    stmt = (
        select(FileResource)
        .where(FileResource.id == file_resource_id)
        .options(
            selectinload(FileResource.series).selectinload(TVSeries.episodes),
            selectinload(FileResource.series).selectinload(TVSeries.collection),
            selectinload(FileResource.movie).selectinload(Movie.collection),
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_notification_for_task(db, task: DownloadTask) -> DownloadNotification:
    """Create the notification for a completed task (idempotent).

    Stops the torrent (best-effort) and snapshots the torrent's file listing
    via the downloader RPC (best-effort — on failure the row is enqueued
    without ``files`` and the consumer falls back to scanning the download
    directory itself). Does not commit; the caller owns the transaction.
    """
    existing = (
        await db.execute(
            select(DownloadNotification).where(
                DownloadNotification.download_task_id == task.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    agent = await db.get(Agent, task.agent_id) if task.agent_id else None
    resource = await _load_resource(db, task.file_resource_id)

    torrent_info: dict | None = None
    if task.transmission_torrent_id is not None and task.downloader_id:
        downloader = await db.get(DownloaderInstance, task.downloader_id)
        if downloader is not None:
            from app.clients.downloader import get_downloader_client

            wrapper = get_downloader_client(downloader)
            # Stop seeding before the consumer moves the files (best-effort).
            try:
                await wrapper.pause_torrent(task.transmission_torrent_id)
            except Exception as e:
                logger.warning(
                    "[notify] pause torrent %s failed: %s",
                    task.transmission_torrent_id, e,
                )
            try:
                torrent_info = await wrapper.get_torrent_files(
                    task.transmission_torrent_id
                )
            except Exception as e:
                logger.warning(
                    "[notify] file listing for torrent %s unavailable: %s",
                    task.transmission_torrent_id, e,
                )

    notification = DownloadNotification(
        # Python-side column defaults only apply at flush time; the payload
        # embeds the id, so assign it explicitly here.
        id=str(uuid.uuid4()),
        agent_id=task.agent_id,
        download_task_id=task.id,
        payload={},  # placeholder, filled below once the id exists
        status="pending",
        next_attempt_at=utcnow(),
    )
    notification.payload = build_payload(
        notification.id, agent, task, resource, torrent_info
    )
    db.add(notification)
    return notification


async def deliver_due_notifications(db) -> dict:
    """Deliver every due ``pending`` notification to its Agent's webhook.

    - mock webhook: delivery counts as success without any HTTP call (payload
      inspection only; a mock consumer never acks, so the torrent is kept).
    - no webhook registered: the row waits in the queue; delivery resumes
      automatically once a webhook is registered.
    - HTTP failure: exponential backoff; exhausting ``notify_max_attempts``
      flips the row to ``failed`` (recoverable via manual UI retry).

    Commits per notification so the DB write lock is never held across an
    HTTP call.
    """
    now = utcnow()
    stmt = (
        select(DownloadNotification)
        .where(
            DownloadNotification.status == "pending",
            (
                DownloadNotification.next_attempt_at.is_(None)
                | (DownloadNotification.next_attempt_at <= now)
            ),
        )
        .order_by(DownloadNotification.created_at.asc())
        .limit(_DELIVERY_BATCH)
    )
    due = (await db.execute(stmt)).scalars().all()
    stats = {"delivered": 0, "failed": 0, "skipped": 0}

    for n in due:
        agent = await db.get(Agent, n.agent_id) if n.agent_id else None
        if agent is None:
            stats["skipped"] += 1
            continue
        if agent.notify_webhook_mock:
            n.notified_at = utcnow()
            n.error_message = None
            stats["delivered"] += 1
            await db.commit()
            continue
        if not agent.notify_webhook_url:
            stats["skipped"] += 1
            continue
        try:
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    agent.notify_webhook_url,
                    json={"event": "download.completed", "notification": n.payload},
                )
                resp.raise_for_status()
            n.notified_at = utcnow()
            n.error_message = None
            stats["delivered"] += 1
        except Exception as e:
            n.attempt_count += 1
            if n.attempt_count >= settings.notify_max_attempts:
                n.status = "failed"
                n.error_message = (
                    f"webhook 投递失败（已达最大重试次数）: {e}"[:2000]
                )
                stats["failed"] += 1
            else:
                n.next_attempt_at = utcnow() + backoff_delay(n.attempt_count)
                n.error_message = f"webhook 投递失败，将退避重试: {e}"[:2000]
                stats["skipped"] += 1
        await db.commit()

    return stats


async def backfill_notifications(
    db, agent_id: str, since: datetime | None
) -> int:
    """Create notifications for completed tasks that never had one.

    ``since=None`` checks from the earliest completed task. Commits in
    batches; delivery is left to the per-minute delivery loop, which
    naturally rate-limits the webhook calls.
    """
    notified_task_ids = (
        select(DownloadNotification.download_task_id)
        .where(DownloadNotification.agent_id == agent_id)
        .scalar_subquery()
    )
    stmt = (
        select(DownloadTask)
        .where(
            DownloadTask.agent_id == agent_id,
            DownloadTask.status == "completed",
            DownloadTask.id.notin_(notified_task_ids),
        )
        .order_by(DownloadTask.completed_at.asc().nulls_first())
    )
    if since is not None:
        stmt = stmt.where(DownloadTask.completed_at >= since)
    tasks = (await db.execute(stmt)).scalars().all()

    created = 0
    for task in tasks:
        await create_notification_for_task(db, task)
        created += 1
        if created % _BACKFILL_COMMIT_EVERY == 0:
            await db.commit()
    await db.commit()
    return created


def reset_for_retry(notification: DownloadNotification) -> None:
    """Manual UI retry: make the row due for immediate redelivery."""
    notification.status = "pending"
    notification.attempt_count = 0
    notification.next_attempt_at = utcnow()
    notification.error_message = None
