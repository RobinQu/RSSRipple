"""Fetch service: RSS fetch → parse → store → metadata pipeline."""

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rss_parser import (
    _parse_feed_sync,
    _entry_to_dict,
    _extract_download_urls,
    _extract_published_at,
)
from app.utils.time import utcnow
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.services.metadata_service import (
    apply_title_extraction,
    extract_search_title,
    fetch_and_link_metadata,
)
from app.services.resource_parser import parse_entry

logger = logging.getLogger(__name__)

# Columns that are set explicitly in the FileResource(...) constructor.
_EXPLICIT_RESOURCE_COLS = frozenset({
    "id", "channel_id", "guid", "title_raw",
    "torrent_url", "detail_url", "published_at", "parsed_at",
    "created_at",
})


async def fetch_channel_resources(channel: Channel, db: AsyncSession) -> dict:
    """Fetch RSS feed for a channel, parse entries, store new FileResources,
    link metadata, and enqueue agent runs for active agents.

    Returns dict with counts + list of new resource IDs.
    """
    channel.last_fetch_status = "running"
    channel.last_fetch_error = None

    # 1. Fetch RSS (30s timeout enforced by asyncio.wait_for)
    try:
        feed = await asyncio.wait_for(
            asyncio.to_thread(_parse_feed_sync, channel.url),
            timeout=30,
        )
    except Exception as e:
        logger.warning("[fetch:%s] feed fetch failed: %s", channel.id, e)
        channel.status = "error"
        channel.last_fetch_status = "failed"
        channel.last_fetch_error = str(e)[:2000]
        return {"new_resource_ids": [], "new_count": 0, "error": str(e)}

    if feed.bozo and not feed.entries:
        exc = getattr(feed, "bozo_exception", None)
        msg = f"Failed to fetch RSS feed '{channel.url}': {exc or 'unknown error'}"
        logger.warning("[fetch:%s] %s", channel.id, msg)
        channel.status = "error"
        channel.last_fetch_status = "failed"
        channel.last_fetch_error = msg
        return {"new_resource_ids": [], "new_count": 0, "error": msg}

    entries = feed.entries
    logger.debug("[fetch:%s] Feed read: %d entries", channel.id, len(entries))
    if not entries:
        channel.last_fetched_at = utcnow()
        channel.last_fetch_status = "success"
        channel.status = "active"
        channel.last_fetch_error = None
        return {"new_resource_ids": [], "new_count": 0}

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
        torrent_url = fm_torrent_url or torrent_url_auto
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
        db.add(resource)
        await db.flush()

        # Title backfill
        base_title = extract_search_title(resource)
        if base_title:
            try:
                cleaned = await apply_title_extraction(
                    base_title,
                    channel.title_extraction_method,
                    channel.title_extraction_regex,
                    db,
                )
                resource.search_title = cleaned or base_title
            except Exception as e:
                logger.warning("Title extraction failed for %s: %s", resource.id, e)
                resource.search_title = base_title
        else:
            resource.search_title = None

        # Metadata linking
        try:
            await fetch_and_link_metadata(db, resource, channel)
        except Exception as e:
            logger.warning("Metadata linking failed for %s: %s", resource.id, e)

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

    # Finalize channel status
    channel.last_fetched_at = utcnow()
    channel.last_fetch_status = "success"
    channel.status = "active"
    channel.last_fetch_error = None

    await db.flush()

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
        "total": len(entries),
        "new_count": new_count,
        "new_resource_ids": new_resource_ids,
    }
