"""Resolve torrent file listings and apply them through one shared pipeline."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.services.torrent_inspect import fetch_torrent_file_detailed, parse_torrent_files
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

ListingSource = Literal["torrent_cache", "torrent_fetch", "downloader", "notification", "none"]

_FETCH_RETRY_BASE_SECONDS = 60
_FETCH_RETRY_MAX_SECONDS = 3600
_HTTP_FETCH_STALE_SECONDS = 300
_RECONCILE_RETRY_BASE_SECONDS = 60
_RECONCILE_RETRY_MAX_SECONDS = 3600
_RECONCILE_STALE_SECONDS = 3600


@dataclass(slots=True)
class ResourceListing:
    files: list[dict] = field(default_factory=list)
    source: ListingSource = "none"
    provider: str | None = None
    error: str | None = None
    name: str | None = None

    @property
    def kind(self) -> Literal["torrent", "listing"] | None:
        if self.source in ("torrent_cache", "torrent_fetch"):
            return "torrent"
        if self.source in ("downloader", "notification"):
            return "listing"
        return None


def _error_text(error: object) -> str:
    return str(error)[:2048] or error.__class__.__name__


def _with_error(listing: ResourceListing, provider: str, error: object) -> None:
    message = f"{provider}: {_error_text(error)}"
    listing.error = f"{listing.error}; {message}" if listing.error else message
    if listing.provider is None:
        listing.provider = provider


async def claim_torrent_fetch(
    db: AsyncSession,
    resource_id: str,
    *,
    expected_attempt_id: str | None = None,
    force: bool = False,
) -> str | None:
    """Atomically claim an HTTP fetch; commit before starting network work."""
    attempt_id = str(uuid.uuid4())
    now = utcnow()
    predicates = [
        FileResource.id == resource_id,
        or_(
            FileResource.torrent_fetch_status.is_(None),
            FileResource.torrent_fetch_status.notin_(("pending", "running")),
            and_(
                FileResource.torrent_fetch_status.in_(("pending", "running")),
                or_(
                    FileResource.torrent_fetch_updated_at.is_(None),
                    FileResource.torrent_fetch_updated_at < now - timedelta(
                        seconds=_HTTP_FETCH_STALE_SECONDS
                    ),
                ),
            ),
        ),
    ]
    if expected_attempt_id is not None:
        uuid.UUID(expected_attempt_id)
        predicates.append(FileResource.torrent_fetch_attempt_id == expected_attempt_id)
    if not force:
        predicates.append(or_(
            FileResource.torrent_fetch_status.is_(None),
            FileResource.torrent_fetch_status != "failed",
            FileResource.torrent_fetch_next_attempt_at <= now,
        ))
    result = await db.execute(
        update(FileResource)
        .where(*predicates)
        .values(
            torrent_fetch_status="running",
            torrent_fetch_attempt_id=attempt_id,
            torrent_fetch_attempts=FileResource.torrent_fetch_attempts + 1,
            torrent_fetch_error=None,
            torrent_fetch_updated_at=now,
            torrent_fetch_next_attempt_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    return attempt_id if result.rowcount else None


async def record_torrent_fetch_success(
    db: AsyncSession,
    resource_id: str,
    attempt_id: str,
    torrent_path: str,
    *,
    expected_torrent_file: str | None,
) -> bool:
    """Publish only when this attempt and the original cache reference still win."""
    uuid.UUID(attempt_id)
    result = await db.execute(
        update(FileResource)
        .where(
            FileResource.id == resource_id,
            FileResource.torrent_fetch_attempt_id == attempt_id,
            FileResource.torrent_fetch_status == "running",
            FileResource.torrent_file == expected_torrent_file,
        )
        .values(
            torrent_file=torrent_path,
            torrent_fetch_status="done",
            torrent_fetch_error=None,
            torrent_fetch_updated_at=utcnow(),
            torrent_fetch_next_attempt_at=None,
            listing_reconcile_status="pending",
            listing_reconcile_attempt_id=None,
            listing_reconcile_error=None,
            listing_reconcile_updated_at=utcnow(),
            listing_reconcile_next_attempt_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def record_torrent_fetch_failure(
    db: AsyncSession,
    resource_id: str,
    attempt_id: str,
    error: object,
    *,
    retryable: bool,
) -> bool:
    """Record a fenced failure and schedule only retryable failures.

    ``fetch_torrent_file_detailed`` supplies the classification; permanent
    errors retain ``next_attempt_at=NULL`` and require an explicit forced retry.
    """
    uuid.UUID(attempt_id)
    attempts = await db.scalar(
        select(FileResource.torrent_fetch_attempts).where(
            FileResource.id == resource_id,
            FileResource.torrent_fetch_attempt_id == attempt_id,
            FileResource.torrent_fetch_status == "running",
        )
    )
    if attempts is None:
        return False
    next_attempt_at = None
    if retryable:
        delay = min(
            _FETCH_RETRY_MAX_SECONDS,
            _FETCH_RETRY_BASE_SECONDS * 2 ** min(max(attempts - 1, 0), 6),
        )
        next_attempt_at = utcnow() + timedelta(seconds=delay)
    result = await db.execute(
        update(FileResource)
        .where(
            FileResource.id == resource_id,
            FileResource.torrent_fetch_attempt_id == attempt_id,
            FileResource.torrent_fetch_status == "running",
        )
        .values(
            torrent_fetch_status="failed",
            torrent_fetch_error=_error_text(error),
            torrent_fetch_updated_at=utcnow(),
            torrent_fetch_next_attempt_at=next_attempt_at,
        )
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def fetch_resource_torrent(
    db: AsyncSession, resource: FileResource, *, force: bool = False
) -> ResourceListing:
    """Fetch an HTTP torrent with a committed claim and classified result."""
    result = ResourceListing()
    cached = resource.torrent_file
    if cached and await asyncio.to_thread(Path(cached).exists):
        files = await asyncio.to_thread(parse_torrent_files, cached)
        if files is not None:
            return ResourceListing(
                files=files, source="torrent_cache", provider="torrent_cache"
            )
    url = resource.torrent_url or ""
    if not url.startswith(("http://", "https://")):
        return result
    attempt_id = await claim_torrent_fetch(db, resource.id, force=force)
    if attempt_id is None:
        if resource.torrent_fetch_status in ("pending", "running"):
            _with_error(result, "http", "fetch already in progress")
        elif resource.torrent_fetch_error:
            _with_error(result, "http", resource.torrent_fetch_error)
        else:
            _with_error(result, "http", "fetch is waiting for its next attempt")
        return result

    # Other workers must see the claim before any outbound request starts.
    await db.commit()
    try:
        path = await fetch_torrent_file_detailed(url, resource.id)
        files = await asyncio.to_thread(parse_torrent_files, path)
        if files is None:
            raise RuntimeError("cached torrent could not be parsed")
    except Exception as error:  # noqa: BLE001 - record the failure before provider fallback
        recorded = await record_torrent_fetch_failure(
            db, resource.id, attempt_id, error,
            retryable=bool(getattr(error, "retryable", False)),
        )
        await db.commit()
        if recorded:
            await db.refresh(resource)
        _with_error(result, "http", error)
        return result

    if not await record_torrent_fetch_success(
        db, resource.id, attempt_id, path, expected_torrent_file=cached
    ):
        await db.rollback()
        _with_error(result, "http", "fetch attempt was replaced")
        return result
    await db.commit()
    resource.torrent_file = path
    return ResourceListing(files=files, source="torrent_fetch", provider="http")


async def _from_downloader(
    db: AsyncSession,
    resource: FileResource,
    result: ResourceListing,
    preferred_task_id: str | None,
    only_task_id: str | None,
) -> ResourceListing | None:
    from app.clients.downloader import get_downloader_client

    predicates = [
        DownloadTask.file_resource_id == resource.id,
        DownloadTask.transmission_torrent_id.isnot(None),
    ]
    if only_task_id is not None:
        predicates.append(DownloadTask.id == only_task_id)
    tasks = (await db.scalars(
        select(DownloadTask)
        .where(*predicates)
        .order_by(
            case((DownloadTask.id == preferred_task_id, 0), else_=1),
            DownloadTask.created_at.desc(),
        )
    )).all()
    for task in tasks:
        downloader = await db.get(DownloaderInstance, task.downloader_id)
        if downloader is None:
            continue
        try:
            info = await get_downloader_client(downloader).get_torrent_files(
                task.transmission_torrent_id
            )
        except Exception as error:  # noqa: BLE001 - try every historical task
            _with_error(result, downloader.type, error)
            continue
        files = (info or {}).get("files") or []
        if files:
            return ResourceListing(
                files=files,
                source="downloader",
                provider=downloader.type,
                name=info.get("name"),
            )
    return None


async def _from_notification(
    db: AsyncSession, resource: FileResource
) -> ResourceListing | None:
    notifications = (await db.scalars(
        select(DownloadNotification)
        .join(DownloadTask, DownloadNotification.download_task_id == DownloadTask.id)
        .where(DownloadTask.file_resource_id == resource.id)
        .order_by(DownloadNotification.created_at.desc())
    )).all()
    for notification in notifications:
        files = (notification.payload or {}).get("files") or []
        if files:
            return ResourceListing(
                files=files, source="notification", provider="notification"
            )
    return None


async def resolve_resource_listing(
    db: AsyncSession,
    resource: FileResource,
    *,
    fetch_http: bool = True,
    preferred_task_id: str | None = None,
    only_task_id: str | None = None,
    include_notifications: bool = True,
) -> ResourceListing:
    """Resolve cache → HTTP → every task RPC → notification snapshots."""
    result = ResourceListing()
    cached = resource.torrent_file
    if cached and await asyncio.to_thread(Path(cached).exists):
        files = await asyncio.to_thread(parse_torrent_files, cached)
        if files is not None:
            return ResourceListing(
                files=files, source="torrent_cache", provider="torrent_cache"
            )
        _with_error(result, "torrent_cache", "cached torrent could not be parsed")

    if fetch_http:
        fetched = await fetch_resource_torrent(db, resource)
        if fetched.files:
            return fetched
        if fetched.error:
            result.error = f"{result.error}; {fetched.error}" if result.error else fetched.error
            if result.provider is None:
                result.provider = fetched.provider

    downloaded = await _from_downloader(
        db, resource, result, preferred_task_id, only_task_id
    )
    if downloaded is not None:
        return downloaded
    if include_notifications:
        snapshot = await _from_notification(db, resource)
        if snapshot is not None:
            return snapshot
    return result


async def _converge_batch_graph(db: AsyncSession, resource: FileResource, channel) -> None:
    """Run graph convergence; strict callers persist any failure for retry."""
    from app.services.torrent_inspect import has_manual_listing_shape

    if has_manual_listing_shape(resource):
        return
    if not resource.is_batch or resource.batch_scope not in ("franchise", "multi_season"):
        return
    from app.services.bangumi_relations import expand_bangumi_series_graph
    from app.services.batch_content_analysis import rebind_absolute_season_assignments
    from app.services.cluster_work_binding import bind_hint_clusters
    from app.services.franchise_service import (
        dedupe_resource_movies,
        enforce_franchise_resource_invariant,
    )

    await expand_bangumi_series_graph(db, resource)
    await rebind_absolute_season_assignments(db, resource)
    enforce_franchise_resource_invariant(resource)
    await bind_hint_clusters(db, resource, channel)
    async with db.begin_nested():
        await dedupe_resource_movies(db, resource, channel=channel)


async def apply_resource_listing(
    db: AsyncSession,
    resource: FileResource,
    files: list[dict],
    *,
    publish: bool = False,
) -> list[str]:
    """Apply the shared report pipeline and optionally publish this transaction."""
    if not files:
        return []
    from app.models.channel import Channel
    from app.services.batch_content_analysis import bind_single_work_assignments
    from app.services.torrent_inspect import (
        analyze_torrent_files,
        apply_torrent_report,
        has_manual_listing_shape,
    )

    channel = await db.get(Channel, resource.channel_id)
    manual_shape = has_manual_listing_shape(resource)
    report = await asyncio.to_thread(analyze_torrent_files, files)
    await apply_torrent_report(db, resource, report, channel, strict=publish)
    await db.flush()
    if not manual_shape:
        await bind_single_work_assignments(db, resource)
    if publish and not manual_shape:
        from app.services.declared_season_links import link_declared_seasons

        await link_declared_seasons(db, resource)
        await _converge_batch_graph(db, resource, channel)
        await bind_single_work_assignments(db, resource)
    if publish:
        from app.services.resource_publication import publish_resource

        await publish_resource(db, resource.id, kind="metadata")
    return []


async def mark_listing_reconcile_pending(
    db: AsyncSession, resource_id: str, *, force: bool = False
) -> bool:
    """Persist needed work without interrupting an active or completed attempt."""
    now = utcnow()
    predicates = [
        FileResource.id == resource_id,
        or_(
            FileResource.listing_reconcile_status.is_(None),
            FileResource.listing_reconcile_status.notin_(("pending", "running")),
        ),
    ]
    if not force:
        predicates.append(or_(
            FileResource.listing_reconcile_status.is_(None),
            FileResource.listing_reconcile_status != "done",
        ))
    else:
        predicates.append(or_(
            FileResource.listing_reconcile_status.is_(None),
            FileResource.listing_reconcile_status == "done",
            FileResource.listing_reconcile_status == "failed",
        ))
    result = await db.execute(
        update(FileResource)
        .where(*predicates)
        .values(
            listing_reconcile_status="pending",
            listing_reconcile_attempt_id=None,
            listing_reconcile_error=None,
            listing_reconcile_updated_at=now,
            listing_reconcile_next_attempt_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def enqueue_listing_reconcile(resource_id: str) -> bool:
    from app.services.task_queue import task_queue

    queued = await task_queue.enqueue(
        "reconcile_resource_listing", f"listing-reconcile:{resource_id}",
        {"resource_id": resource_id},
    )
    return queued is not None


async def schedule_listing_reconcile(
    db: AsyncSession, resource_id: str, *, force: bool = False
) -> bool:
    """Commit pending work before its queue hint; a false queue result is recoverable."""
    if not await mark_listing_reconcile_pending(db, resource_id, force=force):
        return False
    await db.commit()
    return await enqueue_listing_reconcile(resource_id)


async def _claim_listing_reconcile(
    db: AsyncSession, resource_id: str, *, force: bool = False
) -> str | None:
    now = utcnow()
    attempt_id = str(uuid.uuid4())
    stale_before = now - timedelta(seconds=_RECONCILE_STALE_SECONDS)
    predicates = [
        FileResource.id == resource_id,
        or_(
            FileResource.listing_reconcile_status.is_(None),
            FileResource.listing_reconcile_status.notin_(
                ("pending", "running") if force else ("pending", "running", "done")
            ),
            and_(
                FileResource.listing_reconcile_status == "pending",
            ),
            and_(
                FileResource.listing_reconcile_status == "running",
                or_(
                    FileResource.listing_reconcile_updated_at.is_(None),
                    FileResource.listing_reconcile_updated_at < stale_before,
                ),
            ),
        ),
    ]
    if not force:
        predicates.extend([
            or_(
                FileResource.listing_reconcile_status.is_(None),
                FileResource.listing_reconcile_status != "done",
            ),
            or_(
                FileResource.listing_reconcile_status.is_(None),
                FileResource.listing_reconcile_status != "failed",
                FileResource.listing_reconcile_next_attempt_at <= now,
            ),
        ])
    result = await db.execute(
        update(FileResource)
        .where(*predicates)
        .values(
            listing_reconcile_status="running",
            listing_reconcile_attempt_id=attempt_id,
            listing_reconcile_attempts=FileResource.listing_reconcile_attempts + 1,
            listing_reconcile_error=None,
            listing_reconcile_updated_at=now,
            listing_reconcile_next_attempt_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    return attempt_id if result.rowcount else None


async def _record_listing_reconcile_failure(
    resource_id: str, attempt_id: str, error: object
) -> bool:
    from app.database import committed_session

    now = utcnow()
    async with committed_session() as db:
        attempts = await db.scalar(
            select(FileResource.listing_reconcile_attempts).where(
                FileResource.id == resource_id,
                FileResource.listing_reconcile_attempt_id == attempt_id,
                FileResource.listing_reconcile_status == "running",
            )
        )
        if attempts is None:
            return False
        delay = min(
            _RECONCILE_RETRY_MAX_SECONDS,
            _RECONCILE_RETRY_BASE_SECONDS * 2 ** min(max(attempts - 1, 0), 6),
        )
        result = await db.execute(
            update(FileResource)
            .where(
                FileResource.id == resource_id,
                FileResource.listing_reconcile_attempt_id == attempt_id,
                FileResource.listing_reconcile_status == "running",
            )
            .values(
                listing_reconcile_status="failed",
                listing_reconcile_error=_error_text(error),
                listing_reconcile_updated_at=now,
                listing_reconcile_next_attempt_at=now + timedelta(seconds=delay),
            )
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)


async def reconcile_resource(
    resource_id: str,
    *,
    files: list[dict] | None = None,
    expected_magnet_attempt_id: str | None = None,
    force: bool = False,
) -> ResourceListing:
    """Durably apply a listing, publish metadata, then dispatch after commit."""
    from app.database import committed_session

    if expected_magnet_attempt_id is not None:
        uuid.UUID(expected_magnet_attempt_id)
    listing = ResourceListing(
        files=files or [], source="torrent_cache" if files is not None else "none",
        provider="caller" if files is not None else None,
    )
    attempt_id = None
    try:
        async with committed_session() as db:
            resource = await db.get(FileResource, resource_id)
            if resource is None:
                return ResourceListing(error="resource not found")
            if (
                expected_magnet_attempt_id is not None
                and resource.magnet_resolve_attempt_id != expected_magnet_attempt_id
            ):
                return ResourceListing(provider="magnet", error="resolution attempt was replaced")
            attempt_id = await _claim_listing_reconcile(db, resource_id, force=force)
            if attempt_id is None:
                if resource.listing_reconcile_status != "done":
                    listing.error = resource.listing_reconcile_error or (
                        "reconcile is active or waiting for retry"
                    )
                return listing
            # The running claim must survive graph/network work and process crashes.
            await db.commit()
            await db.refresh(resource, ["file_assignments", "work_links"])
            if files is None:
                listing = await resolve_resource_listing(db, resource, fetch_http=False)
            if not listing.files:
                raise RuntimeError(listing.error or "no resource listing is available")
            await apply_resource_listing(db, resource, listing.files, publish=True)
            predicates = [
                FileResource.id == resource_id,
                FileResource.listing_reconcile_attempt_id == attempt_id,
                FileResource.listing_reconcile_status == "running",
            ]
            if expected_magnet_attempt_id is not None:
                predicates.extend([
                    FileResource.magnet_resolve_attempt_id == expected_magnet_attempt_id,
                    FileResource.magnet_resolve_status == "done",
                ])
            owned = await db.execute(
                update(FileResource)
                .where(*predicates)
                .values(
                    listing_reconcile_status="done",
                    listing_reconcile_error=None,
                    listing_reconcile_updated_at=utcnow(),
                    listing_reconcile_next_attempt_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if not owned.rowcount:
                raise RuntimeError("listing reconcile attempt was replaced")
    except Exception as error:  # noqa: BLE001 - durable state owns the retry
        logger.warning("[listing] reconcile failed for %s: %s", resource_id, error, exc_info=True)
        if attempt_id is not None:
            await _record_listing_reconcile_failure(resource_id, attempt_id, error)
        listing.error = _error_text(error)
        return listing

    from app.services.publication_dispatch import dispatch_pending_publications

    try:
        await dispatch_pending_publications()
    except Exception:  # noqa: BLE001 - committed publications retain the wakeup
        logger.warning("[listing] publication dispatch failed for %s", resource_id, exc_info=True)
    return listing


async def retry_resource_torrent(resource_id: str) -> dict:
    """Queue-handler entry for one due HTTP fetch, followed by reconciliation."""
    from app.database import committed_session
    from app.services.task_queue import require_execution_ownership

    await require_execution_ownership()
    magnet_url = None
    async with committed_session() as db:
        resource = await db.get(FileResource, resource_id)
        if resource is None:
            return {"fetched": False, "error": "resource not found"}
        listing = await fetch_resource_torrent(db, resource)
        magnet_url = resource.magnet_url
    await require_execution_ownership()
    if listing.files:
        await reconcile_resource(resource_id, files=listing.files)
        return {"fetched": True, "error": None}
    if magnet_url:
        from app.services import magnet_resolve

        await magnet_resolve.enqueue_resolution(resource_id)
    return {"fetched": False, "error": listing.error}
