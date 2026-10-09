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

from app.models.agent_run import AgentRun
from app.models.agent_suggestion import AgentSuggestion
from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.pending_decision import PendingDecision
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


async def scrub_deleted_resource_ids(db: AsyncSession, resource_ids) -> dict[str, int]:
    """Scrub dangling references to deleted FileResource ids from JSON lists.

    ``AgentRun.matched_resource_ids``, ``AgentSuggestion.resources`` and
    ``PendingDecision.candidates`` hold resource ids inside JSON arrays with
    no FK, so deleting a FileResource would otherwise leave dangling ids
    behind. Each affected row is read, filtered and written back
    (dialect-agnostic; a 36-char UUID cannot be a substring of another id,
    so the LIKE prefilter is exact enough and Python does the precise
    filter). Suggestions left with no resources are deleted (mirroring
    ``_persist_suggestions``, which never persists empty groups). Pending
    decisions that drop below the two-candidate minimum expire — the same
    terminal state the scheduler's time-based expiry uses.
    Returns per-table changed-row counts.
    """
    from sqlalchemy import String, cast, or_

    ids = {str(r) for r in resource_ids}
    counts = {"agent_runs": 0, "agent_suggestions": 0, "pending_decisions": 0}
    if not ids:
        return counts

    def _chunks(seq, size=100):
        seq = sorted(seq)
        for i in range(0, len(seq), size):
            yield seq[i:i + size]

    async def _fetch(model, column):
        rows = {}
        for chunk in _chunks(ids):
            prefilter = or_(*(cast(column, String).like(f"%{rid}%") for rid in chunk))
            for row in (await db.scalars(select(model).where(prefilter))).all():
                rows[row.id] = row
        return list(rows.values())

    for run in await _fetch(AgentRun, AgentRun.matched_resource_ids):
        current = list(run.matched_resource_ids or [])
        kept = [r for r in current if r not in ids]
        if kept != current:
            run.matched_resource_ids = kept
            counts["agent_runs"] += 1

    for suggestion in await _fetch(AgentSuggestion, AgentSuggestion.resources):
        current = list(suggestion.resources or [])
        kept = [r for r in current if r not in ids]
        if kept == current:
            continue
        counts["agent_suggestions"] += 1
        if kept:
            suggestion.resources = kept
        else:
            await db.delete(suggestion)

    for decision in await _fetch(PendingDecision, PendingDecision.candidates):
        current = list(decision.candidates or [])
        kept = [r for r in current if r not in ids]
        if kept == current:
            continue
        counts["pending_decisions"] += 1
        decision.candidates = kept
        if decision.llm_picked_resource_id in ids:
            decision.llm_picked_resource_id = None
            decision.llm_suggestion = None
        if decision.status == "pending" and len(kept) < 2:
            # A pending choice requires >= 2 candidates; with fewer left the
            # conflict is gone, so expire rather than strand an undecidable row.
            decision.status = "expired"
    return counts


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
    candidate_ids: list[str] = []
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
            candidate_ids.extend(ids)
            deleted += result.rowcount or 0
    else:
        # Turso keeps the guarded deletion in its existing write transaction.
        candidate_ids = list((await db.scalars(
            select(FileResource.id).where(predicate)
        )).all())
        if candidate_ids:
            result = await db.execute(
                delete(FileResource).where(FileResource.id.in_(candidate_ids), predicate)
            )
            deleted = result.rowcount or 0
        else:
            deleted = 0
    if deleted:
        # Scrub JSON id-list references for the rows that are actually gone
        # (a row whose predicate match changed mid-flight is kept and must
        # keep its JSON references too).
        surviving = set((await db.scalars(
            select(FileResource.id).where(FileResource.id.in_(candidate_ids))
        )).all())
        gone = [rid for rid in candidate_ids if rid not in surviving]
        scrubbed = await scrub_deleted_resource_ids(db, gone)
        logger.info(
            "[cleanup] channel %s: deleted %d unresolved resources older than %d days",
            channel_id, deleted, days,
        )
        if any(scrubbed.values()):
            logger.info(
                "[cleanup] channel %s: scrubbed deleted ids from %d run(s), "
                "%d suggestion(s), %d pending decision(s)",
                channel_id, scrubbed["agent_runs"],
                scrubbed["agent_suggestions"], scrubbed["pending_decisions"],
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
