"""Fetch service: RSS fetch → parse → store → metadata pipeline."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rss_parser import (
    _entry_to_dict,
    _extract_download_urls,
    _extract_published_at,
    _parse_feed_sync,
)
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.services.metadata_service import fetch_and_link_metadata
from app.services.resource_parser import parse_entry
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# ── Metadata backfill knobs ──
# Existing FileResources are skipped by the GUID dedup above, so without a
# backfill phase a transient metadata failure (timeout / LLM-format error) or
# a source that later improves would never get a second chance. Each fetch
# re-runs up to ``MAX_BACKFILL_PER_FETCH`` retry-eligible unmatched resources.
MAX_BACKFILL_PER_FETCH = 30
# How many unmatched rows to load for eligibility filtering. Ordered
# oldest-attempt-first, so the most-eligible rows come first; scanning a bit
# beyond the cap absorbs ordering ties without loading the whole backlog.
MAX_BACKFILL_SCAN = 100
# Transient failures retry with exponential backoff: 1h, 2h, 4h, … capped.
TRANSIENT_BACKOFF_BASE_HOURS = 1
TRANSIENT_BACKOFF_MAX_HOURS = 24
# A definitive "no match" is retried this long after the last attempt, in case
# the external source has improved its coverage.
NOT_FOUND_RETRY_DAYS = 14


def _simple_title_clean(raw: str) -> str | None:
    """Minimal title cleanup: strip leading bracket group and episode tail."""
    if not raw:
        return None
    import re
    cleaned = re.sub(r"^\[[^\]]*\]\s*", "", raw)
    cleaned = re.sub(r"\s*-\s*\d+\b.*$", "", cleaned)
    return cleaned.strip() or raw.strip()

# Columns that are set explicitly in the FileResource(...) constructor.
_EXPLICIT_RESOURCE_COLS = frozenset({
    "id", "channel_id", "guid", "title_raw",
    "torrent_url", "detail_url", "published_at", "parsed_at",
    "created_at",
})


def _is_retry_eligible(resource: FileResource, now) -> bool:
    """Whether an unmatched resource should be re-run on this fetch.

    Policy (matches the design in the metadata-retry spec):
      * never tried (``metadata_attempts == 0``) → eligible;
      * ``non_work`` (correctly identified as music/ASMR/OP) → never;
      * ``transient`` (timeout/connection/LLM-format) → after exponential
        backoff, capped at ``TRANSIENT_BACKOFF_MAX_HOURS``;
      * ``not_found`` (source had no match) → after ``NOT_FOUND_RETRY_DAYS``;
      * unknown failure type or missing timestamp → eligible (re-evaluate).
    """
    attempts = int(getattr(resource, "metadata_attempts", 0) or 0)
    if attempts == 0:
        return True
    ftype = getattr(resource, "metadata_failure_type", None)
    if ftype == "non_work":
        return False
    last = getattr(resource, "last_metadata_attempt_at", None)
    if last is None:
        return True
    age = now - last
    if ftype == "transient":
        backoff_hours = min(
            TRANSIENT_BACKOFF_BASE_HOURS * (2 ** (attempts - 1)),
            TRANSIENT_BACKOFF_MAX_HOURS,
        )
        return age >= timedelta(hours=backoff_hours)
    if ftype == "not_found":
        return age >= timedelta(days=NOT_FOUND_RETRY_DAYS)
    return True


async def _backfill_unmatched_resources(channel: Channel, db: AsyncSession) -> int:
    """Re-run metadata for retry-eligible unmatched resources of a channel.

    Returns the number of resources re-processed. Bounded by
    ``MAX_BACKFILL_PER_FETCH`` per fetch so the job stays predictable.
    """
    now = utcnow()
    result = await db.execute(
        select(FileResource)
        .where(
            FileResource.channel_id == channel.id,
            FileResource.series_id.is_(None),
            FileResource.movie_id.is_(None),
        )
        .order_by(
            FileResource.last_metadata_attempt_at.asc().nullsfirst(),
            FileResource.created_at.asc(),
        )
        .limit(MAX_BACKFILL_SCAN)
    )
    candidates = result.scalars().all()

    processed = 0
    for resource in candidates:
        if processed >= MAX_BACKFILL_PER_FETCH:
            break
        if not _is_retry_eligible(resource, now):
            continue
        try:
            if channel.metadata_agent_enabled:
                from app.services.metadata_agent import get_agent
                await get_agent().process(resource, channel, db, force_refresh=True)
            else:
                await fetch_and_link_metadata(db, resource, channel)
        except Exception as e:
            logger.warning(
                "[fetch:%s] backfill re-match failed for %s: %s",
                channel.id, resource.id, e,
            )
        processed += 1
        await db.commit()
    return processed



async def fetch_channel_resources(channel: Channel, db: AsyncSession) -> dict:
    """Fetch RSS feed for a channel, parse entries, store new FileResources,
    link metadata, and enqueue agent runs for active agents.

    Returns dict with counts + list of new resource IDs.
    """
    channel.last_fetch_status = "running"
    channel.last_fetch_error = None
    await db.commit()

    # 1. Fetch RSS (30s timeout). A feed outage no longer aborts the job: the
    # backfill (step 3) still re-runs retry-eligible unmatched resources so
    # repair progresses even when the feed is temporarily unreachable.
    feed = None
    feed_error: str | None = None
    try:
        feed = await asyncio.wait_for(
            asyncio.to_thread(_parse_feed_sync, channel.url),
            timeout=30,
        )
    except Exception as e:
        logger.warning("[fetch:%s] feed fetch failed: %s", channel.id, e)
        feed_error = str(e)[:2000]

    if feed is not None and feed.bozo and not feed.entries:
        exc = getattr(feed, "bozo_exception", None)
        feed_error = f"Failed to fetch RSS feed '{channel.url}': {exc or 'unknown error'}"
        logger.warning("[fetch:%s] %s", channel.id, feed_error)
        feed = None

    if feed_error is not None:
        channel.status = "error"
        channel.last_fetch_status = "failed"
        channel.last_fetch_error = feed_error
        await db.commit()

    entries = feed.entries if feed is not None else []
    logger.debug("[fetch:%s] Feed read: %d entries", channel.id, len(entries))
    # Note: an empty feed no longer short-circuits — the backfill phase below
    # still re-runs retry-eligible unmatched resources, so a transiently empty
    # feed still makes repair progress. The new-entry loop is simply a no-op.

    # 2. Existing GUIDs for dedup
    result = await db.execute(
        select(FileResource.guid).where(FileResource.channel_id == channel.id)
    )
    existing_guids = {row[0] for row in result.all()}

    column_names = {c.name for c in FileResource.__table__.columns}
    new_count = 0
    new_resource_ids: list[str] = []

    for entry in entries:
        guid = getattr(entry, "id", None) or entry.get("link") or entry.get("title", "")
        if not guid or guid in existing_guids:
            continue

        title = entry.get("title", "")
        logger.debug("[fetch:%s] New entry: guid=%s title=%r", channel.id, guid, title[:120])

        entry_dict = _entry_to_dict(entry)

        # Parse via field_mapping
        parsed: dict = {}
        if channel.field_mapping:
            try:
                parsed = parse_entry(entry_dict, channel.field_mapping, entry.get("description"))
                parsed = {k: v for k, v in parsed.items() if v is not None}
            except Exception as e:
                logger.debug("[fetch:%s] Field mapping failed: guid=%s error=%s", channel.id, guid, e)
                parsed = {}

        # Pop explicit fields
        fm_torrent_url = parsed.pop("torrent_url", None) or None
        fm_detail_url = parsed.pop("detail_url", None) or None
        fm_published_at = parsed.pop("published_at", None) or None
        fm_title_cn = parsed.pop("title_cn", None) or None
        fm_title_en = parsed.pop("title_en", None) or None

        torrent_url_auto, _ = _extract_download_urls(entry)
        # Prefer auto-detected download URL (enclosures/magnets) over field-mapped,
        # because field_mapping may point to a non-download URL (e.g. detail page <link>).
        torrent_url = torrent_url_auto or fm_torrent_url
        if not torrent_url:
            logger.warning("Skipping entry '%s': no torrent/magnet URL found", guid)
            continue

        detail_url = fm_detail_url or entry.get("link")
        published_at_val = fm_published_at or _extract_published_at(entry)

        resource = FileResource(
            channel_id=channel.id,
            guid=guid,
            title_raw=title,
            torrent_url=torrent_url,
            detail_url=detail_url,
            published_at=published_at_val,
            parsed_at=utcnow(),
            title_cn=fm_title_cn,
            title_en=fm_title_en,
            **{k: v for k, v in parsed.items() if k in column_names and k not in _EXPLICIT_RESOURCE_COLS},
        )
        # Pre-parser: heuristic batch detection from the raw title. Runs before
        # the LLM so downstream logic (filtering, dedup) still sees ``is_batch``
        # even when the metadata agent is disabled or fails. The LLM may later
        # refine these values in ``UnifiedMetadataAgent._apply_to_resource``.
        from app.services.resource_parser import (
            detect_absolute_episode,
            detect_batch,
            detect_subtitle_langs,
        )
        pre_is_batch, pre_start, pre_end = detect_batch(title)
        if pre_is_batch:
            resource.is_batch = True
            if pre_start is not None:
                resource.episode_start = pre_start
            if pre_end is not None:
                resource.episode_end = pre_end
        # Subtitle language pre-fill. Store an empty list (rather than None)
        # once parsed, so downstream code can distinguish "never parsed" from
        # "no explicit marking".
        resource.subtitle_langs = detect_subtitle_langs(title)
        # NN(MM) double-labeled episode. When the title spells out both the
        # per-season number and the absolute count (e.g. "13(85)"), take the
        # per-season one and stash the absolute for audit / manual review.
        # This runs before the LLM so the MetadataAgent never sees the
        # ambiguity — see _reconcile_episode for the LLM-driven case.
        pre_ep, pre_abs = detect_absolute_episode(title)
        if pre_ep is not None:
            resource.episode = pre_ep
            resource.absolute_episode = pre_abs
            resource.episode_confidence = "reconciled"
        db.add(resource)
        await db.flush()
        # Commit the new resource immediately so the SQLite write lock is
        # released *before* the metadata ReAct loop below. agent.process runs
        # many LLM + external search calls (tens of messages per resource when
        # a source misbehaves, e.g. jina 402s) and can take minutes; holding a
        # write transaction open across that blocks every other writer -
        # including channel edits, which surface as "database is locked" /
        # requests that never return. The metadata result is persisted by a
        # fresh short transaction on the next commit.
        await db.commit()

        # Unified metadata agent (title cleaning + metadata search in one call)
        if channel.metadata_agent_enabled:
            try:
                from app.services.metadata_agent import get_agent
                agent = get_agent()
                await agent.process(resource, channel, db)
            except Exception as e:
                logger.warning("MetadataAgent failed for %s: %s", resource.id, e)
                # Fallback: extract a basic search_title and try local match
                base_title = _simple_title_clean(resource.title_raw)
                resource.search_title = base_title
        else:
            # metadata_agent_enabled=False: only local DB match
            try:
                await fetch_and_link_metadata(db, resource, channel)
            except Exception as e:
                logger.warning("Metadata linking failed for %s: %s", resource.id, e)
        # Persist metadata writes and release the write lock before the poster
        # download (another network call) so it too doesn't block other writers.
        await db.commit()

        # Poster download for newly-linked entities
        if resource.series_id:
            from app.models.series import TVSeries
            series = await db.get(TVSeries, resource.series_id)
            if series and series.poster_url and series.poster_url.startswith("http"):
                from app.services.metadata_service import download_and_cache_poster
                local = await download_and_cache_poster(series.poster_url)
                if local:
                    series.poster_url = local
        elif resource.movie_id:
            from app.models.movie import Movie
            movie = await db.get(Movie, resource.movie_id)
            if movie and movie.poster_url and movie.poster_url.startswith("http"):
                from app.services.metadata_service import download_and_cache_poster
                local = await download_and_cache_poster(movie.poster_url)
                if local:
                    movie.poster_url = local

        existing_guids.add(guid)
        new_resource_ids.append(resource.id)
        new_count += 1
        await db.commit()
        logger.debug(
            "[fetch:%s] Committed resource %s (%d/%d new so far)",
            channel.id,
            resource.id,
            new_count,
            len(entries),
        )

    # 3. Backfill: re-run metadata for retry-eligible unmatched resources.
    # Existing resources are skipped by the GUID dedup in step 2, so without
    # this phase a transient failure (timeout / LLM-format error) or a source
    # that later improves would never get a second chance.
    backfilled_count = 0
    try:
        backfilled_count = await _backfill_unmatched_resources(channel, db)
    except Exception as e:
        logger.warning("[fetch:%s] backfill phase failed: %s", channel.id, e)

    # Finalize channel status - only mark success when the feed fetch succeeded.
    if feed_error is None:
        channel.last_fetched_at = utcnow()
        channel.last_fetch_status = "success"
        channel.status = "active"
        channel.last_fetch_error = None

    await db.commit()

    # Enqueue agent runs (fire-and-forget)
    from app.services.task_queue import task_queue
    for agent in channel.agents:
        if agent.status != "active":
            continue
        try:
            await task_queue.enqueue(
                "run_agent",
                f"agent:{agent.id}",
                {"agent_id": agent.id},
            )
        except Exception as e:
            logger.warning("Failed to enqueue run_agent for %s: %s", agent.id, e)

    return {
        "status": "error" if feed_error is not None else ("success" if new_count > 0 else "unchanged"),
        "total": len(entries),
        "new_count": new_count,
        "new_resource_ids": new_resource_ids,
        "backfilled_count": backfilled_count,
        "error": feed_error,
    }
