"""Channel API routes."""

import json
import logging

from fastapi import APIRouter, Depends, Header, Query

logger = logging.getLogger(__name__)
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.channel import Channel
from app.schemas.channel import ChannelCreate, ChannelUpdate, ChannelResponse, ValidateURLRequest, PreviewFeedRequest
from app.schemas.common import success_response, paginated_response
from app.clients.rss_parser import get_raw_entries, validate_rss_url
from app.services.feed_analyzer import analyze_feed
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter()


@router.get("/channels")
async def list_channels(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    total_q = await db.execute(select(func.count()).select_from(Channel))
    total = total_q.scalar_one()
    result = await db.execute(
        select(Channel).order_by(Channel.created_at.desc()).offset(offset).limit(page_size)
    )
    channels = result.scalars().all()
    return paginated_response(
        [ChannelResponse.model_validate(c).model_dump() for c in channels],
        total=total, page=page, page_size=page_size,
    )


@router.post("/channels", status_code=201)
async def create_channel(
    body: ChannelCreate,
    db: AsyncSession = Depends(get_db),
    x_form_token: str | None = Header(default=None),
):
    if x_form_token is not None:
        from app.services.submission_guard import submission_guard
        if not await submission_guard.consume(x_form_token):
            return JSONResponse(
                status_code=409,
                content={"success": False, "data": None, "error": {"code": "DUPLICATE_SUBMISSION", "message": "This form was already submitted. Please reload the page and try again."}},
            )
    # Validate the RSS feed before creating
    try:
        is_valid, feed_msg, item_count, downloadable_count = await validate_rss_url(body.url)
    except Exception:
        logger.error("validate_rss_url failed for url=%s", body.url, exc_info=True)
        raise

    if not is_valid:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "INVALID_FEED",
                    "message": feed_msg,
                },
            },
        )

    channel = Channel(**body.model_dump())
    db.add(channel)
    await db.flush()
    await db.refresh(channel)
    return success_response(
        ChannelResponse.model_validate(channel).model_dump(),
        meta={"feed_items": item_count, "downloadable": downloadable_count},
    )


@router.get("/channels/form-token")
async def get_form_token():
    """Issue a single-use submission token (synchronizer token pattern).

    The frontend requests a token when the Create/Edit Channel form loads and
    includes it in the subsequent POST/PUT via the X-Form-Token header.  The
    server consumes the token on first use; a second request with the same
    token is rejected with 409 DUPLICATE_SUBMISSION.
    """
    from app.services.submission_guard import submission_guard
    token = await submission_guard.issue()
    return success_response({"token": token})


@router.get("/channels/{channel_id}")
async def get_channel(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(Channel, channel_id)
    if not channel:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Channel not found"}})
    return success_response(ChannelResponse.model_validate(channel).model_dump())


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
            return JSONResponse(
                status_code=409,
                content={"success": False, "data": None, "error": {"code": "DUPLICATE_SUBMISSION", "message": "This form was already submitted. Please reload the page and try again."}},
            )
    channel = await db.get(Channel, channel_id)
    if not channel:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Channel not found"}})
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(channel, key, value)
    await db.flush()
    await db.refresh(channel)
    return success_response(ChannelResponse.model_validate(channel).model_dump())


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str, db: AsyncSession = Depends(get_db)):
    channel = await db.get(Channel, channel_id)
    if not channel:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Channel not found"}})
    await db.delete(channel)
    return success_response({"deleted": True})


@router.post("/channels/{channel_id}/fetch")
async def fetch_channel(channel_id: str, db: AsyncSession = Depends(get_db)):
    """Enqueue a background fetch for the channel.

    Returns immediately with job status.  Use GET /fetch-status to poll progress.
    Returns 409 (with current job state) if a fetch is already in progress.
    """
    channel = await db.get(Channel, channel_id)
    if not channel:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Channel not found"}})

    from app.services.task_queue import task_queue

    job = await task_queue.enqueue("fetch_channel", channel_id, {"channel_id": channel_id})
    if job is None:
        current = await task_queue.status(channel_id)
        return JSONResponse(
            status_code=409,
            content={"success": False, "data": current, "error": {"code": "ALREADY_RUNNING", "message": "A fetch is already in progress for this channel"}},
        )

    return success_response(job)


@router.get("/channels/{channel_id}/fetch-status")
async def get_fetch_status(channel_id: str, db: AsyncSession = Depends(get_db)):
    """Poll the status of the latest fetch job for a channel."""
    channel = await db.get(Channel, channel_id)
    if not channel:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Channel not found"}})

    from app.services.task_queue import task_queue
    return success_response(await task_queue.status(channel_id))


@router.post("/channels/{channel_id}/analyze")
async def analyze_channel_feed(channel_id: str, db: AsyncSession = Depends(get_db)):
    """Analyze RSS feed using LLM to generate field mappings.

    Fetches sample entries, sends them to the LLM, and returns proposed field mappings.
    Does NOT auto-save the mapping - returns it for user review.
    """
    channel = await db.get(Channel, channel_id)
    if not channel:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Channel not found"}})

    try:
        entries = await get_raw_entries(channel.url, limit=5)
    except Exception as e:
        return JSONResponse(status_code=400, content={"success": False, "data": None, "error": {"code": "FETCH_ERROR", "message": str(e)}})

    if not entries:
        return JSONResponse(status_code=400, content={"success": False, "data": None, "error": {"code": "EMPTY_FEED", "message": "No entries found in RSS feed"}})

    result = await analyze_feed(entries)
    return success_response(result)


@router.post("/channels/validate-url")
async def validate_url(body: ValidateURLRequest):
    """Validate that an RSS URL is reachable and has downloadable content."""
    is_valid, message, item_count, downloadable_count = await validate_rss_url(body.url)
    return success_response({
        "valid": is_valid,
        "message": message,
        "item_count": item_count,
        "downloadable_count": downloadable_count,
    })


@router.post("/channels/preview-feed")
async def preview_feed(body: PreviewFeedRequest):
    """Preview RSS feed entries and optionally parse them with a field mapping.

    Fetches up to 20 raw entries from the given URL. If ``field_mapping`` is
    provided, each entry is also parsed using ``resource_parser.parse_entry``
    so the caller can see how the current rules would extract structured
    fields. Used by the Channel form's live preview pane.
    """
    try:
        entries = await get_raw_entries(body.url, limit=20)
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "data": None,
                "error": {"code": "FETCH_ERROR", "message": str(e)},
            },
        )

    parsed: list[dict] = []
    if body.field_mapping and entries:
        from app.services.resource_parser import parse_entry
        for entry in entries:
            parsed.append(parse_entry(entry, body.field_mapping))

    return success_response({
        "entries": entries,
        "parsed": parsed,
    })


@router.post("/channels/analyze-url-stream")
async def analyze_url_stream(body: ValidateURLRequest):
    """Stream LLM analysis for a feed URL — no existing channel required.

    Used by the Create Channel form so users can generate field mappings
    before saving the channel. Identical to the channel-based endpoint but
    accepts a URL in the request body instead of looking up a channel.
    """
    try:
        entries = await get_raw_entries(body.url, limit=5)
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"success": False, "data": None, "error": {"code": "FETCH_ERROR", "message": str(e)}},
        )

    if not entries:
        return JSONResponse(
            status_code=400,
            content={"success": False, "data": None, "error": {"code": "EMPTY_FEED", "message": "No entries found in RSS feed"}},
        )

    from app.services.feed_analyzer import analyze_feed_stream

    async def event_generator():
        async for event in analyze_feed_stream(entries):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/channels/{channel_id}/analyze-stream")
async def analyze_channel_feed_stream(channel_id: str, db: AsyncSession = Depends(get_db)):
    """Stream LLM analysis of RSS feed as Server-Sent Events."""
    channel = await db.get(Channel, channel_id)
    if not channel:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Channel not found"}})

    try:
        entries = await get_raw_entries(channel.url, limit=5)
    except Exception as e:
        return JSONResponse(status_code=400, content={"success": False, "data": None, "error": {"code": "FETCH_ERROR", "message": str(e)}})

    if not entries:
        return JSONResponse(status_code=400, content={"success": False, "data": None, "error": {"code": "EMPTY_FEED", "message": "No entries found in RSS feed"}})

    from app.services.feed_analyzer import analyze_feed_stream

    async def event_generator():
        async for event in analyze_feed_stream(entries):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/channels/{channel_id}/generate-title-regex")
async def generate_title_regex(channel_id: str, db: AsyncSession = Depends(get_db)):
    """Generate a title cleanup regex via LLM based on feed content.

    Fetches sample entries from the channel's RSS feed, sends the titles
    to the LLM, and returns a regex pattern that extracts the core work
    title from each entry. The user can then review and edit the regex
    in the Channel form.

    Returns ``{ "regex": "..." }`` or an error if the LLM fails.
    """
    channel = await db.get(Channel, channel_id)
    if not channel:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Channel not found"}})

    try:
        entries = await get_raw_entries(channel.url, limit=10)
    except Exception as e:
        return JSONResponse(status_code=400, content={"success": False, "data": None, "error": {"code": "FETCH_ERROR", "message": str(e)}})

    if not entries:
        return JSONResponse(status_code=400, content={"success": False, "data": None, "error": {"code": "EMPTY_FEED", "message": "No entries found in RSS feed"}})

    from app.services.title_cleaner import generate_title_regex as _gen_regex

    regex = await _gen_regex(entries)
    if not regex:
        return JSONResponse(status_code=500, content={"success": False, "data": None, "error": {"code": "LLM_ERROR", "message": "LLM failed to generate a regex. Check that the LLM API key is configured."}})

    return success_response({"regex": regex})
