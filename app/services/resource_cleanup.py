"""Automatic cleanup of stale unresolved FileResources.

A channel may opt into auto-cleanup (``auto_cleanup_unresolved_enabled``) with a
configurable age threshold (``auto_cleanup_unresolved_days``, default 21 = 3
weeks). The daily scheduler job calls :func:`cleanup_stale_unresolved_resources`
to sweep every opted-in channel; :func:`cleanup_channel_unresolved_resources`
is the single-channel entry point exposed via the manual API trigger.

A resource is deleted when ALL hold:
  * it belongs to a channel with auto-cleanup enabled,
  * it has no linked work (``series_id``/``movie_id``/``audio_work_id`` all
    NULL), no collection or work links, and ``metadata_matched_at IS NULL``,
  * it has no bound file assignments or manually curated file placements,
  * it has had no manual handling: ``episode_confidence != 'manual'`` and no
    ``DownloadTask`` references it (a download was initiated - for an
    unresolved resource this means a direct/manual download, since agents only
    auto-download matched resources),
  * ``created_at`` is older than the channel's threshold.

Deletion only removes the RSS-item DB record (never downloaded files); if the
feed re-publishes the same ``guid`` the resource is re-created and re-matched.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


def _stale_unresolved_where(channel: Channel, cutoff):
    """WHERE clause for stale, un-handled, unresolved resources on ``channel``.

    ``DownloadTask`` is nullable=False on ``file_resource_id`` with
    ``ondelete=CASCADE``; the NOT EXISTS guard both protects any in-flight
    downloads and avoids cascading their rows.
    """
    from sqlalchemy import and_, exists, or_

    has_download = exists(
        select(DownloadTask.id).where(
            DownloadTask.file_resource_id == FileResource.id
        )
    )
    has_work_link = exists(
        select(ResourceWorkLink.id).where(ResourceWorkLink.resource_id == FileResource.id)
    )
    has_handled_assignment = exists(
        select(ResourceFileAssignment.id).where(
            ResourceFileAssignment.resource_id == FileResource.id,
            or_(
                ResourceFileAssignment.series_id.is_not(None),
                ResourceFileAssignment.movie_id.is_not(None),
                ResourceFileAssignment.source == "manual",
            ),
        )
    )
    return and_(
        FileResource.channel_id == channel.id,
        FileResource.series_id.is_(None),
        FileResource.movie_id.is_(None),
        FileResource.audio_work_id.is_(None),
        FileResource.collection_id.is_(None),
        ~has_work_link,
        ~has_handled_assignment,
        FileResource.metadata_matched_at.is_(None),
        FileResource.created_at < cutoff,
        # "!= 'manual' including NULL" — IS NOT <literal> is SQLite-only
        # syntax; IS DISTINCT FROM renders correctly on both backends.
        FileResource.episode_confidence.is_distinct_from("manual"),
        ~has_download,
    )


async def cleanup_channel_unresolved_resources(
    db: AsyncSession, channel_id: str, *, force: bool = False
) -> int:
    """Delete stale unresolved resources for one channel.

    Returns the number of rows deleted. When ``force`` is False (the default)
    and the channel has auto-cleanup disabled, nothing is deleted - this is the
    path the automatic daily job uses. ``force=True`` (the manual API trigger)
    runs regardless of the toggle, using the channel's configured threshold (or
    the default if unset), so an admin can clean a channel that hasn't opted in.
    """
    channel = await db.get(Channel, channel_id)
    if channel is None:
        return 0
    if not force and not channel.auto_cleanup_unresolved_enabled:
        return 0

    days = channel.auto_cleanup_unresolved_days or 21
    cutoff = utcnow() - timedelta(days=days)
    predicate = _stale_unresolved_where(channel, cutoff)
    if db.get_bind().dialect.name == "postgresql":
        deleted = 0
        after_id = None
        while True:
            # Child FK writes take KEY SHARE on their parent. A DELETE that
            # waits for them can cascade newly committed associations using
            # an older statement snapshot. Lock candidates first, skipping
            # in-flight editors, then recheck in a fresh READ COMMITTED
            # statement while the parent lock prevents new child writes.
            candidates = select(FileResource.id).where(predicate)
            if after_id is not None:
                candidates = candidates.where(FileResource.id > after_id)
            ids = (await db.scalars(
                candidates.order_by(FileResource.id).limit(500)
                .with_for_update(of=FileResource, skip_locked=True)
            )).all()
            if not ids:
                break
            after_id = ids[-1]
            result = await db.execute(
                delete(FileResource).where(FileResource.id.in_(ids), predicate)
            )
            deleted += result.rowcount or 0
    else:
        # Turso keeps the guarded deletion in its existing write transaction.
        result = await db.execute(delete(FileResource).where(predicate))
        deleted = result.rowcount or 0
    if deleted:
        logger.info(
            "[cleanup] channel %s: deleted %d unresolved resources older than %d days",
            channel_id, deleted, days,
        )
    return deleted


async def cleanup_stale_unresolved_resources(db: AsyncSession) -> dict:
    """Sweep every channel with auto-cleanup enabled. Returns a summary."""
    channels = (
        await db.execute(
            select(Channel).where(Channel.auto_cleanup_unresolved_enabled.is_(True))
        )
    ).scalars().all()
    total = 0
    for ch in channels:
        total += await cleanup_channel_unresolved_resources(db, ch.id)
    if total:
        logger.info(
            "[cleanup] auto-cleanup deleted %d resources across %d channels",
            total, len(channels),
        )
    return {"channels": len(channels), "deleted": total}
