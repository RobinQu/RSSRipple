"""FileResource API routes."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from fastapi.responses import JSONResponse

from app.database import get_db
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from app.schemas.file_resource import (
    FileResourceResponse,
    GroupedResource,
    MetadataSearchRequest,
    MetadataSearchResult,
    MetadataLinkRequest,
)
from app.schemas.common import success_response, paginated_response
from app.services.metadata_service import (
    fetch_and_link_metadata,
    manual_search_metadata,
    manual_link_metadata,
)
from app.services.task_queue import task_queue

router = APIRouter()


@router.get("/channels/{channel_id}/resources")
async def list_resources(
    channel_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    grouped: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    ch = await db.get(Channel, channel_id)
    if not ch:
        return JSONResponse(
            status_code=404,
            content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Channel not found"}},
        )
    base_q = select(FileResource).where(FileResource.channel_id == channel_id)
    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()

    if grouped:
        result = await db.execute(
            base_q.options(selectinload(FileResource.series), selectinload(FileResource.movie))
            .order_by(FileResource.published_at.desc())
        )
        resources = result.scalars().all()
        groups: dict[tuple, list] = {}
        unknown_key = ("unknown", None)
        groups[unknown_key] = []
        for r in resources:
            if r.series_id and r.series:
                key = ("series", r.series_id)
            elif r.movie_id and r.movie:
                key = ("movie", r.movie_id)
            else:
                groups[unknown_key].append(r)
                continue
            groups.setdefault(key, []).append(r)

        out = []
        for key, items in groups.items():
            if key == unknown_key:
                out.append({
                    "type": "unknown", "id": None, "title": "未识别",
                    "poster_url": None,
                    "resources": [FileResourceResponse.model_validate(r).model_dump() for r in items],
                })
                continue
            t, tid = key
            if t == "series":
                s = items[0].series
                out.append({
                    "type": "series", "id": tid,
                    "title": s.title_cn or s.title_en or s.original_title or tid,
                    "poster_url": s.poster_url,
                    "resources": [FileResourceResponse.model_validate(r).model_dump() for r in items],
                })
            else:
                m = items[0].movie
                out.append({
                    "type": "movie", "id": tid,
                    "title": m.title_cn or m.title_en or m.original_title or tid,
                    "poster_url": m.poster_url,
                    "resources": [FileResourceResponse.model_validate(r).model_dump() for r in items],
                })
        return success_response({"groups": out}, meta={"total": total})

    offset = (page - 1) * page_size
    result = await db.execute(
        base_q.options(selectinload(FileResource.series), selectinload(FileResource.movie))
        .order_by(FileResource.published_at.desc())
        .offset(offset).limit(page_size)
    )
    resources = result.scalars().all()
    return paginated_response(
        [FileResourceResponse.model_validate(r).model_dump() for r in resources],
        total=total, page=page, page_size=page_size,
    )


@router.get("/resources/{resource_id}")
async def get_resource(resource_id: str, db: AsyncSession = Depends(get_db)):
    resource = await db.get(
        FileResource, resource_id,
        options=[selectinload(FileResource.series), selectinload(FileResource.movie)],
    )
    if not resource:
        return JSONResponse(
            status_code=404,
            content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Resource not found"}},
        )
    return success_response(FileResourceResponse.model_validate(resource).model_dump())


@router.get("/resources/{resource_id}/metadata")
async def get_resource_metadata(resource_id: str, db: AsyncSession = Depends(get_db)):
    resource = await db.get(
        FileResource, resource_id,
        options=[selectinload(FileResource.series), selectinload(FileResource.movie)],
    )
    if not resource:
        return JSONResponse(
            status_code=404,
            content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Resource not found"}},
        )

    channel = await db.get(Channel, resource.channel_id)
    try:
        await fetch_and_link_metadata(db, resource, channel)
        await db.commit()
    except Exception as e:
        await db.rollback()
        return JSONResponse(
            status_code=500,
            content={"success": False, "data": None, "error": {"code": "INTERNAL_SERVER_ERROR", "message": str(e)}},
        )

    await db.refresh(resource, ["series", "movie"])
    linked = None
    if resource.series:
        from app.schemas.series import TVSeriesResponse
        linked = {"type": "series", "entity": TVSeriesResponse.model_validate(resource.series).model_dump()}
    elif resource.movie:
        from app.schemas.movie import MovieResponse
        linked = {"type": "movie", "entity": MovieResponse.model_validate(resource.movie).model_dump()}

    return success_response({
        "resource_id": resource.id,
        "series_id": resource.series_id,
        "movie_id": resource.movie_id,
        "metadata_matched_at": resource.metadata_matched_at.isoformat() if resource.metadata_matched_at else None,
        "linked": linked,
    })


@router.post("/resources/{resource_id}/metadata/search")
async def search_metadata(
    resource_id: str, body: MetadataSearchRequest, db: AsyncSession = Depends(get_db)
):
    resource = await db.get(FileResource, resource_id)
    if not resource:
        return JSONResponse(
            status_code=404,
            content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Resource not found"}},
        )
    try:
        results = await manual_search_metadata(db, body.search_title, body.content_type)
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"success": False, "data": None, "error": {"code": "LLM_ERROR", "message": str(e)}},
        )
    return success_response({"results": [MetadataSearchResult(**r).model_dump() for r in results]})


@router.put("/resources/{resource_id}/metadata/link")
async def link_metadata(
    resource_id: str, body: MetadataLinkRequest, db: AsyncSession = Depends(get_db)
):
    resource = await db.get(FileResource, resource_id)
    if not resource:
        return JSONResponse(
            status_code=404,
            content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Resource not found"}},
        )
    channel = await db.get(Channel, resource.channel_id)
    try:
        entity = await manual_link_metadata(db, resource, channel, body.selected_result)
        await db.commit()
    except Exception as e:
        await db.rollback()
        return JSONResponse(
            status_code=500,
            content={"success": False, "data": None, "error": {"code": "INTERNAL_SERVER_ERROR", "message": str(e)}},
        )

    # Re-trigger agent runs for the channel
    for agent in channel.agents:
        if agent.status == "active":
            try:
                await task_queue.enqueue("run_agent", f"agent:{agent.id}", {"agent_id": agent.id})
            except Exception:
                pass

    # Expire and re-fetch resource with relationships eager-loaded so Pydantic
    # serialization works without triggering implicit lazy IO.
    await db.flush()
    resource = (await db.execute(
        select(FileResource)
        .where(FileResource.id == resource.id)
        .options(selectinload(FileResource.series), selectinload(FileResource.movie))
    )).scalar_one()
    return success_response(FileResourceResponse.model_validate(resource).model_dump())
