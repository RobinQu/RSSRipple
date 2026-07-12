"""Channel API routes."""

import json
import logging
from collections import Counter

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.rss_parser import get_raw_entries, validate_rss_url
from app.database import get_db
from app.models.channel import Channel
from app.schemas.channel import (
    ChannelCreate,
    ChannelResponse,
    ChannelUpdate,
    PreviewFeedRequest,
    SummarizeFiltersRequest,
    ValidateURLRequest,
)
from app.schemas.common import paginated_response, success_response
from app.services.feed_analyzer import analyze_feed, analyze_feed_stream

logger = logging.getLogger(__name__)

router = APIRouter()


def _not_found(message: str = "Channel not found"):
    return JSONResponse(
        status_code=404,
        content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": message}, "meta": {}},
    )


def _already_running(existing_job: dict | None = None):
    return JSONResponse(
        status_code=409,
        content={
            "success": False,
            "data": existing_job,
            "error": {"code": "ALREADY_RUNNING", "message": "A job is already running for this channel"},
            "meta": {},
        },
    )


def _valid_field_mapping(mapping: dict | None) -> bool:
    if not isinstance(mapping, dict):
        return False
    field_mappings = mapping.get("field_mappings")
    if isinstance(field_mappings, dict):
        return len(field_mappings) > 0
    return any(isinstance(v, dict) and v.get("source") for v in mapping.values())


@router.get("/channels")
async def list_channels(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    from app.models.agent import Agent
    from app.models.file_resource import FileResource
    offset = (page - 1) * page_size
    total_q = await db.execute(select(func.count()).select_from(Channel))
    total = total_q.scalar_one()
    result = await db.execute(
        select(Channel).order_by(Channel.created_at.desc()).offset(offset).limit(page_size)
    )
    channels = result.scalars().all()
    items = []
    for c in channels:
        d = ChannelResponse.model_validate(c).model_dump()
        # Count agents
        ac = await db.execute(select(func.count()).select_from(Agent).where(Agent.channel_id == c.id))
        rc = await db.execute(select(func.count()).select_from(FileResource).where(FileResource.channel_id == c.id))
        d["agent_count"] = ac.scalar_one() or 0
        d["resource_count"] = rc.scalar_one() or 0
        items.append(d)
    return paginated_response(items, total=total, page=page, page_size=page_size)


@router.post("/channels", status_code=201)
async def create_channel(
    body: ChannelCreate,
    db: AsyncSession = Depends(get_db),
    x_form_token: str | None = Header(default=None),
):
    if not _valid_field_mapping(body.field_mapping):
        return JSONResponse(status_code=422, content={
            "success": False,
            "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": "field_mapping is required"},
            "meta": {},
        })

    if x_form_token is not None:
        from app.services.submission_guard import submission_guard
        if not await submission_guard.consume(x_form_token):
            return JSONResponse(status_code=409, content={
                "success": False, "data": None,
                "error": {"code": "DUPLICATE_SUBMISSION", "message": "This form was already submitted."},
                "meta": {},
            })

    try:
        is_valid, feed_msg, item_count, downloadable_count = await validate_rss_url(body.url)
    except Exception:
        logger.error("validate_rss_url failed for url=%s", body.url, exc_info=True)
        raise

    if not is_valid:
        return JSONResponse(status_code=422, content={
            "success": False, "data": None,
            "error": {"code": "INVALID_FEED", "message": feed_msg}, "meta": {},
        })

    channel = Channel(**body.model_dump())
    db.add(channel)
    await db.flush()
    await db.refresh(channel)

    # Schedule the channel if active
    try:
        from app.services.scheduler import reschedule_channel
        reschedule_channel(channel)
    except Exception:
        logger.debug("Scheduler not ready; skipping schedule for new channel", exc_info=True)

    # Auto-trigger an initial fetch so the new channel starts pulling data
    # immediately instead of waiting for the first scheduler tick. Fire-and-
    # forget: a failure here must not fail the create.
    fetch_triggered = False
    try:
        from app.services.task_queue import task_queue
        job = await task_queue.enqueue(
            "fetch_channel", f"channel:{channel.id}", {"channel_id": channel.id}
        )
        fetch_triggered = job is not None
    except Exception:
        logger.warning(
            "Failed to enqueue initial fetch for channel %s", channel.id, exc_info=True
        )

    return success_response(
        ChannelResponse.model_validate(channel).model_dump(),
        meta={
            "feed_items": item_count,
            "downloadable": downloadable_count,
            "fetch_triggered": fetch_triggered,
        },
    )


@router.get("/channels/form-token")
async def get_form_token():
    from app.services.submission_guard import submission_guard
    return success_response({"token": await submission_guard.issue()})


@router.get("/channels/metadata-sources")
async def list_metadata_sources():
    """Return the external metadata source catalog for the channel form.

    Each source carries ``enabled``/``configured``/``available`` flags. The
    form should offer only ``available`` sources as selectable candidates.
    """
    from app.services.metadata_agent import (
        DEFAULT_METADATA_SOURCE,
        get_metadata_source_catalog,
    )
    return success_response({
        "sources": get_metadata_source_catalog(),
        "default": DEFAULT_METADATA_SOURCE,
    })


@router.get("/channels/{channel_id}")
async def get_channel(channel_id: str, db: AsyncSession = Depends(get_db)):
    from sqlalchemy.orm import selectinload

    from app.models.file_resource import FileResource
    from app.schemas.file_resource import FileResourceResponse
    channel = await db.get(Channel, channel_id)
    if not channel:
        return _not_found()
    base = ChannelResponse.model_validate(channel).model_dump()
    # Recent 20 resources preview
    res = await db.execute(
        select(FileResource)
        .where(FileResource.channel_id == channel_id)
        .options(selectinload(FileResource.series), selectinload(FileResource.movie))
        .order_by(FileResource.published_at.desc())
        .limit(20)
    )
    recent = res.scalars().all()
    base["recent_resources"] = [
        FileResourceResponse.model_validate(r).model_dump() for r in recent
    ]
    return success_response(base)


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: str,
    body: ChannelUpdate,
    db: AsyncSession = Depends(get_db),
    x_form_token: str | None = Header(default=None),
):
    if x_form_token is not None:
        from app.services.submission_guard import submission_guard
        if not await submission_guard.consume(x_form_token):
            return JSONResponse(status_code=409, content={
                "success": False, "data": None,
                "error": {"code": "DUPLICATE_SUBMISSION", "message": "This form was already submitted."},
                "meta": {},
            })
    channel = await db.get(Channel, channel_id)
    if not channel:
        return _not_found()
    old_metadata_source = channel.metadata_source
    update_data = body.model_dump(exclude_unset=True)
    if "field_mapping" in update_data and not _valid_field_mapping(update_data.get("field_mapping")):
        return JSONResponse(status_code=422, content={
            "success": False,
            "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": "field_mapping is required"},
            "meta": {},
        })
    for key, value in update_data.items():
        setattr(channel, key, value)
    await db.flush()

    # If the metadata source changed, clear the old source's not_found/transient
    # cooldowns on this channel's unmatched resources so the backfill reprocesses
    # them under the new source instead of waiting out NOT_FOUND_RETRY_DAYS.
    if channel.metadata_source != old_metadata_source:
        from app.services.fetch_service import reset_channel_metadata_for_source_change
        await reset_channel_metadata_for_source_change(db, channel_id)

    await db.refresh(channel)

    try:
        from app.services.scheduler import reschedule_channel
        reschedule_channel(channel)
    except Exception:
        logger.debug("Scheduler not ready; skipping reschedule", exc_info=True)

    return success_response(ChannelResponse.model_validate(channel).model_dump())


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(Channel, channel_id)
    if not channel:
        return _not_found()
    try:
        from app.services.scheduler import unschedule_channel
        unschedule_channel(channel_id)
    except Exception:
        pass
    await db.delete(channel)
    return success_response({"deleted": True})


@router.post("/channels/{channel_id}/fetch")
async def fetch_channel(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(Channel, channel_id)
    if not channel:
        return _not_found()
    from app.services.task_queue import task_queue
    job = await task_queue.enqueue("fetch_channel", f"channel:{channel_id}", {"channel_id": channel_id})
    if job is None:
        existing = await task_queue.status(f"channel:{channel_id}")
        return _already_running(existing)
    return success_response(job)


@router.get("/channels/{channel_id}/fetch-status")
async def get_fetch_status(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(Channel, channel_id)
    if not channel:
        return _not_found()
    from app.services.task_queue import task_queue
    return success_response(await task_queue.status(f"channel:{channel_id}"))


@router.post("/channels/{channel_id}/analyze")
async def analyze_channel_feed(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(Channel, channel_id)
    if not channel:
        return _not_found()
    try:
        entries = await get_raw_entries(channel.url, limit=5)
    except Exception as e:
        return JSONResponse(status_code=400, content={
            "success": False, "data": None, "error": {"code": "FETCH_ERROR", "message": str(e)}, "meta": {},
        })
    if not entries:
        return JSONResponse(status_code=400, content={
            "success": False, "data": None, "error": {"code": "EMPTY_FEED", "message": "No entries found"}, "meta": {},
        })
    return success_response(await analyze_feed(entries))


@router.post("/channels/validate-url")
async def validate_url(body: ValidateURLRequest):
    is_valid, message, item_count, downloadable_count = await validate_rss_url(body.url)
    return success_response({
        "valid": is_valid, "message": message,
        "item_count": item_count, "downloadable_count": downloadable_count,
    })


@router.post("/channels/preview-feed")
async def preview_feed(body: PreviewFeedRequest):
    try:
        entries = await get_raw_entries(body.url, limit=20)
    except Exception as e:
        return JSONResponse(status_code=400, content={
            "success": False, "data": None, "error": {"code": "FETCH_ERROR", "message": str(e)}, "meta": {},
        })
    parsed = []
    if body.field_mapping and entries:
        from app.services.resource_parser import parse_entry
        for entry in entries:
            parsed.append(parse_entry(entry, body.field_mapping))
    return success_response({"entries": entries, "parsed": parsed})


async def _stream_events(gen):
    async for event in gen:
        yield f"data: {json.dumps(event)}\n\n"


@router.post("/channels/analyze-url-stream")
async def analyze_url_stream(body: ValidateURLRequest):
    try:
        entries = await get_raw_entries(body.url, limit=5)
    except Exception as e:
        return JSONResponse(status_code=400, content={
            "success": False, "data": None, "error": {"code": "FETCH_ERROR", "message": str(e)}, "meta": {},
        })
    if not entries:
        return JSONResponse(status_code=400, content={
            "success": False, "data": None, "error": {"code": "EMPTY_FEED", "message": "No entries found"}, "meta": {},
        })
    return StreamingResponse(
        _stream_events(analyze_feed_stream(entries)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/channels/{channel_id}/analyze-stream")
async def analyze_channel_stream(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(Channel, channel_id)
    if not channel:
        return _not_found()
    try:
        entries = await get_raw_entries(channel.url, limit=5)
    except Exception as e:
        return JSONResponse(status_code=400, content={
            "success": False, "data": None, "error": {"code": "FETCH_ERROR", "message": str(e)}, "meta": {},
        })
    if not entries:
        return JSONResponse(status_code=400, content={
            "success": False, "data": None, "error": {"code": "EMPTY_FEED", "message": "No entries found"}, "meta": {},
        })
    return StreamingResponse(
        _stream_events(analyze_feed_stream(entries)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/channels/{channel_id}/summarize-filters")
async def summarize_filters(channel_id: str, body: SummarizeFiltersRequest, db: AsyncSession = Depends(get_db)):
    from app.models.file_resource import FileResource
    if not body.resource_ids:
        return success_response({"filter_config": None, "explanation": ""})
    result = await db.execute(
        select(FileResource).where(
            FileResource.channel_id == channel_id,
            FileResource.id.in_(body.resource_ids),
        )
    )
    resources = result.scalars().all()
    if not resources:
        return success_response({"filter_config": None, "explanation": ""})
    conditions = []
    explanation_parts = []
    n = len(resources)
    exact_fields = [
        "subtitle_group",
        "resolution",
        "video_codec",
        "audio_codec",
        "container",
        "subtitle_type",
        "source",
    ]
    for field in exact_fields:
        values = [getattr(r, field) for r in resources if getattr(r, field)]
        if not values:
            continue
        most_common, count = Counter(values).most_common(1)[0]
        if count / n >= 0.8:
            conditions.append({"field": field, "operator": "eq", "value": most_common})
            explanation_parts.append(f"{field}={most_common}")
    filter_config = {"combinator": "and", "conditions": conditions} if conditions else None
    return success_response({"filter_config": filter_config, "explanation": "; ".join(explanation_parts)})
