"""Download notification service.

Builds the per-task notification snapshot and fans it out to the Agent's
registered webhooks as individual ``WebhookDelivery`` rows, each delivered
with its own exponential backoff. RSSRipple's responsibility ends at
"persist the notification and deliver it" — all file planning/execution
lives in the external consumer (vault-organizer). See
docs/design/notifications.md.

Delivery has a single code path: the scheduler's per-minute tick calls
:func:`ensure_deliveries` (create missing fan-out rows) and then
:func:`deliver_due_deliveries`, which picks up every due ``pending``
delivery — fresh ones (``next_attempt_at`` set to now at creation), backoff
retries, and manual UI retries (which reset rows to due-now). Nothing else
sends webhooks.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.agent import Agent
from app.models.agent_webhook import AgentWebhook
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.webhook_delivery import WebhookDelivery
from app.services.genre_registry import normalize_genres
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 1800  # 30 min cap on the exponential backoff
WEBHOOK_TIMEOUT_SECONDS = 180.0
_DELIVERY_BATCH = 50  # max deliveries attempted per scheduler tick
_DELIVERY_CONCURRENCY = 10  # max simultaneous webhook HTTP calls
_REGENERATE_COMMIT_EVERY = 20


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
            # Normalized at snapshot time so the payload always carries the
            # closed TMDB set even for pre-unification rows.
            "genre": normalize_genres(series.genre),
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
            "genre": normalize_genres(movie.genre),
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


async def _find_by_task(
    db, download_task_id: str
) -> DownloadNotification | None:
    return (
        await db.execute(
            select(DownloadNotification).where(
                DownloadNotification.download_task_id == download_task_id
            )
        )
    ).scalar_one_or_none()


async def _build_snapshot(
    db, task: DownloadTask, notification_id: str
) -> tuple[dict, bool]:
    """Run the full notification generation chain for a task: stop seeding
    (best-effort), snapshot the torrent's file listing via the downloader RPC
    (best-effort) and build the frozen payload.

    Returns ``(payload, torrent_snapshot)``. ``torrent_snapshot`` is False
    when no file listing could be obtained (no torrent attached, or RPC
    failure) — the payload then carries no ``files`` and the consumer falls
    back to scanning the download directory itself.
    """
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
    return (
        build_payload(notification_id, agent, task, resource, torrent_info),
        torrent_info is not None,
    )


async def create_notification_for_task(
    db, task: DownloadTask
) -> tuple[DownloadNotification, bool]:
    """Create the notification for a completed task (idempotent).

    Returns ``(notification, created)``. Concurrency-safe: the pre-check +
    insert is not atomic against the per-minute scheduler tick (or a
    concurrent regenerate), so the insert runs in a SAVEPOINT and a lost race
    against the ``download_task_id`` UNIQUE constraint falls back to reading
    the row the concurrent writer committed.

    The payload comes from the full generation chain (see
    :func:`_build_snapshot`). Does not commit; the caller owns the
    transaction.
    """
    existing = await _find_by_task(db, task.id)
    if existing is not None:
        return existing, False

    notification = DownloadNotification(
        # Python-side column defaults only apply at flush time; the payload
        # embeds the id, so assign it explicitly here.
        id=str(uuid.uuid4()),
        agent_id=task.agent_id,
        download_task_id=task.id,
        payload={},  # placeholder, filled below once the id exists
    )
    notification.payload, _ = await _build_snapshot(db, task, notification.id)
    try:
        async with db.begin_nested():  # SAVEPOINT: keep the caller's batch
            db.add(notification)
            await db.flush()
    except IntegrityError:
        # Lost a race against a concurrent creator (scheduler tick / parallel
        # regenerate): the row exists now — adopt it.
        return await _find_by_task(db, task.id), False
    return notification, True


async def ensure_deliveries(db, agent_id: str | None = None) -> int:
    """Fan out: create missing ``pending`` deliveries for every notification
    × every ENABLED webhook of the notification's agent.

    Idempotent via the ``(notification_id, webhook_id)`` unique constraint:
    existing pairs are pre-selected and skipped, and a lost race against a
    concurrent fan-out (scheduler tick vs. webhook registration) is absorbed
    by a SAVEPOINT, like :func:`create_notification_for_task`. A webhook
    registered (or re-enabled) later receives the whole backlog on the next
    run. Commits in batches.
    """
    n_stmt = select(DownloadNotification).where(
        DownloadNotification.agent_id.isnot(None)
    )
    if agent_id is not None:
        n_stmt = n_stmt.where(DownloadNotification.agent_id == agent_id)
    notifications = (await db.execute(n_stmt)).scalars().all()
    if not notifications:
        return 0

    agent_ids = {n.agent_id for n in notifications}
    webhooks = (
        await db.execute(
            select(AgentWebhook).where(
                AgentWebhook.agent_id.in_(agent_ids),
                AgentWebhook.enabled.is_(True),
            )
        )
    ).scalars().all()
    if not webhooks:
        return 0

    by_agent: dict[str, list[AgentWebhook]] = {}
    for w in webhooks:
        by_agent.setdefault(w.agent_id, []).append(w)
    wanted = {
        (n.id, w.id)
        for n in notifications
        for w in by_agent.get(n.agent_id, [])  # type: ignore[arg-type]
    }
    if not wanted:
        return 0

    existing = {
        (row[0], row[1])
        for row in (
            await db.execute(
                select(
                    WebhookDelivery.notification_id, WebhookDelivery.webhook_id
                ).where(
                    WebhookDelivery.notification_id.in_(
                        {n.id for n in notifications}
                    )
                )
            )
        ).all()
    }
    missing = wanted - existing

    created = 0
    for notification_id, webhook_id in sorted(missing):
        delivery = WebhookDelivery(
            notification_id=notification_id,
            webhook_id=webhook_id,
            status="pending",
            next_attempt_at=utcnow(),
        )
        try:
            async with db.begin_nested():  # SAVEPOINT: keep the batch alive
                db.add(delivery)
                await db.flush()
        except IntegrityError:
            continue  # lost a race — the row exists now
        created += 1
        if created % _DELIVERY_BATCH == 0:
            await db.commit()
    await db.commit()
    return created


async def deliver_due_deliveries(db) -> dict:
    """Deliver every due ``pending`` delivery to its webhook.

    - mock webhook: delivery counts as success without any HTTP call
      (payload inspection only).
    - webhook deleted or disabled since fan-out: skipped, stays pending;
      delivery resumes automatically once a webhook is available again.
    - HTTP failure: exponential backoff; exhausting ``notify_max_attempts``
      flips the row to ``failed`` (recoverable via manual UI retry).

    Deliveries run concurrently (bounded by ``_DELIVERY_CONCURRENCY``); each
    delivery commits individually so one failure never rolls back another,
    and the DB write lock is never held across an HTTP call. Row mutation +
    commit are serialized through ``commit_lock``: an AsyncSession is not
    re-entrant, and mutating one row while another delivery's flush is in
    flight would get those changes silently discarded.
    """
    now = utcnow()
    stmt = (
        select(WebhookDelivery)
        .where(
            WebhookDelivery.status == "pending",
            (
                WebhookDelivery.next_attempt_at.is_(None)
                | (WebhookDelivery.next_attempt_at <= now)
            ),
        )
        .options(
            selectinload(WebhookDelivery.notification),
            selectinload(WebhookDelivery.webhook),
        )
        .order_by(WebhookDelivery.created_at.asc())
        .limit(_DELIVERY_BATCH)
    )
    due = (await db.execute(stmt)).scalars().all()
    stats = {"delivered": 0, "failed": 0, "skipped": 0}
    semaphore = asyncio.Semaphore(_DELIVERY_CONCURRENCY)
    commit_lock = asyncio.Lock()

    async def _deliver_one(d: WebhookDelivery) -> None:
        async with semaphore:
            webhook = d.webhook
            if webhook is None or not webhook.enabled:
                stats["skipped"] += 1
                return
            error: Exception | None = None
            if not webhook.mock:
                try:
                    async with httpx.AsyncClient(
                        timeout=WEBHOOK_TIMEOUT_SECONDS
                    ) as client:
                        resp = await client.post(
                            webhook.url,
                            json={
                                "event": "download.completed",
                                "notification": d.notification.payload,
                            },
                        )
                        resp.raise_for_status()
                except Exception as e:
                    error = e
            async with commit_lock:
                if error is None:
                    # HTTP 2xx, or mock webhook (payload inspection only —
                    # no HTTP call at all).
                    d.status = "done"
                    d.delivered_at = utcnow()
                    d.error_message = None
                    stats["delivered"] += 1
                else:
                    d.attempt_count += 1
                    if d.attempt_count >= settings.notify_max_attempts:
                        d.status = "failed"
                        d.error_message = (
                            f"webhook 投递失败（已达最大重试次数）: {error}"[:2000]
                        )
                        stats["failed"] += 1
                    else:
                        d.next_attempt_at = utcnow() + backoff_delay(
                            d.attempt_count
                        )
                        d.error_message = (
                            f"webhook 投递失败，将退避重试: {error}"[:2000]
                        )
                        stats["skipped"] += 1
                await db.commit()

    await asyncio.gather(*(_deliver_one(d) for d in due))
    return stats


async def regenerate_notifications(
    db, agent_id: str, since: datetime | None
) -> dict:
    """Regenerate notifications by re-running the full generation chain
    (:func:`_build_snapshot`) for every completed task of the agent
    (optionally ``completed_at >= since``; ``None`` = from the earliest).

    For each task in scope:

    - no notification yet → create one (same path as the scheduler tick);
    - notification exists → rebuild its payload from scratch and replace it
      (the row — and thus ``payload["notification_id"]`` — is kept), then
      reset its non-``pending`` deliveries to due-immediately ``pending`` so
      consumers receive the regenerated snapshot. If the chain cannot obtain
      a torrent snapshot this time (RPC down / torrent removed / no torrent
      attached), the existing payload is kept as-is: re-running must never
      degrade a previously good snapshot.

    Manual-only (never called by the scheduler tick). Commits in batches;
    fan-out and delivery are left to the per-minute loop, which naturally
    rate-limits the webhook calls. Returns ``{"created", "regenerated"}``.
    """
    stmt = (
        select(DownloadTask)
        .where(
            DownloadTask.agent_id == agent_id,
            DownloadTask.status == "completed",
        )
        .order_by(DownloadTask.completed_at.asc().nulls_first())
    )
    if since is not None:
        stmt = stmt.where(DownloadTask.completed_at >= since)
    tasks = (await db.execute(stmt)).scalars().all()

    stats = {"created": 0, "regenerated": 0}
    for task in tasks:
        existing = (
            await db.execute(
                select(DownloadNotification)
                .where(DownloadNotification.download_task_id == task.id)
                .options(selectinload(DownloadNotification.deliveries))
            )
        ).scalar_one_or_none()
        if existing is None:
            _, was_created = await create_notification_for_task(db, task)
            if was_created:
                stats["created"] += 1
        else:
            payload, has_torrent_snapshot = await _build_snapshot(
                db, task, existing.id
            )
            if not has_torrent_snapshot:
                continue  # keep the old snapshot rather than degrade it
            existing.payload = payload
            now = utcnow()
            for d in existing.deliveries:
                if d.status != "pending":
                    d.status = "pending"
                    d.attempt_count = 0
                    d.next_attempt_at = now
                    d.error_message = None
            stats["regenerated"] += 1
        done = stats["created"] + stats["regenerated"]
        if done and done % _REGENERATE_COMMIT_EVERY == 0:
            await db.commit()
    await db.commit()
    return stats


async def reset_deliveries_for_retry(
    db,
    mode: str,
    since: datetime | None = None,
    agent_id: str | None = None,
    notification_id: str | None = None,
) -> int:
    """Manual retry: make matching deliveries due for immediate redelivery.

    ``mode="failed"`` resets only ``failed`` deliveries; ``mode="all"``
    resets every non-pending delivery (``done`` + ``failed``). Optionally
    scoped by the notification's ``created_at >= since``, ``agent_id``
    and/or a single ``notification_id``. Returns the number reset.
    """
    statuses = ("failed",) if mode == "failed" else ("done", "failed")
    stmt = (
        select(WebhookDelivery)
        .join(
            DownloadNotification,
            WebhookDelivery.notification_id == DownloadNotification.id,
        )
        .where(WebhookDelivery.status.in_(statuses))
    )
    if since is not None:
        stmt = stmt.where(DownloadNotification.created_at >= since)
    if agent_id is not None:
        stmt = stmt.where(DownloadNotification.agent_id == agent_id)
    if notification_id is not None:
        stmt = stmt.where(DownloadNotification.id == notification_id)
    rows = (await db.execute(stmt)).scalars().all()
    for d in rows:
        d.status = "pending"
        d.attempt_count = 0
        d.next_attempt_at = utcnow()
        d.error_message = None
    await db.commit()
    return len(rows)
