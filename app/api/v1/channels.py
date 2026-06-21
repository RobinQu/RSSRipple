"""Channel API routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.channel import Channel
from app.schemas.channel import ChannelCreate, ChannelUpdate, ChannelResponse, ValidateURLRequest
from app.schemas.common import success_response, paginated_response
from app.clients.rss_parser import get_raw_entries, validate_rss_url
from app.services.feed_analyzer import analyze_feed
from fastapi.responses import JSONResponse

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
):
    # Validate the RSS feed before creating
    is_valid, message, item_count, downloadable_count = await validate_rss_url(body.url)
    if not is_valid:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "INVALID_FEED",
                    "message": message,
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
):
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
    channel = await db.get(Channel, channel_id)
    if not channel:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Channel not found"}})
    # TODO: Trigger RSS fetch
    return success_response({"message": "Fetch triggered", "channel_id": channel_id})


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


@router.post("/channels/{channel_id}/apply-mapping")
async def apply_field_mapping(
    channel_id: str,
    body: ChannelUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Apply a field mapping to a channel (after user review of analyze results)."""
    channel = await db.get(Channel, channel_id)
    if not channel:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Channel not found"}})

    if body.field_mapping is not None:
        channel.field_mapping = body.field_mapping
    if body.parser_type is not None:
        channel.parser_type = body.parser_type

    await db.flush()
    await db.refresh(channel)
    return success_response(ChannelResponse.model_validate(channel).model_dump())


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
