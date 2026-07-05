"""FileResource API routes."""

from collections import Counter
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

    if grouped:
        # Paginate by WORK GROUP (not by row). A group is a TVSeries, a Movie,
        # or the synthetic "unknown" bucket for resources without linked
        # metadata. Groups are ordered by their most recent
        # ``published_at`` (or ``created_at`` as a fallback), then paginated;
        # every resource in a group on the current page is returned so the
        # frontend can render the whole work without cross-page splitting.
        pub_col = func.coalesce(FileResource.published_at, FileResource.created_at)

        # Aggregate one row per (series_id / movie_id / unknown) with the max
        # publish time. We do it in three lightweight queries and merge in
        # Python — the group count is bounded (# of works in the channel) so
        # this stays cheap even for feeds with tens of thousands of rows.
        series_groups = (await db.execute(
            select(FileResource.series_id, func.max(pub_col))
            .where(FileResource.channel_id == channel_id, FileResource.series_id.isnot(None))
            .group_by(FileResource.series_id)
        )).all()
        movie_groups = (await db.execute(
            select(FileResource.movie_id, func.max(pub_col))
            .where(FileResource.channel_id == channel_id, FileResource.movie_id.isnot(None))
            .group_by(FileResource.movie_id)
        )).all()
        unknown_last = (await db.execute(
            select(func.max(pub_col), func.count())
            .where(
                FileResource.channel_id == channel_id,
                FileResource.series_id.is_(None),
                FileResource.movie_id.is_(None),
            )
        )).one()

        entries: list[tuple[str, str | None, object]] = []
        for sid, ts in series_groups:
            entries.append(("series", sid, ts))
        for mid, ts in movie_groups:
            entries.append(("movie", mid, ts))
        if unknown_last[1] and unknown_last[1] > 0:
            entries.append(("unknown", None, unknown_last[0]))

        # Sort by last_update desc; None values sink to the end for stability.
        from datetime import datetime as _dt
        _EPOCH = _dt.min
        entries.sort(key=lambda e: e[2] or _EPOCH, reverse=True)

        total_groups = len(entries)
        offset = (page - 1) * page_size
        page_entries = entries[offset : offset + page_size]

        # Load resources for the groups on this page (bulk-load per bucket).
        series_ids_on_page = [tid for typ, tid, _ in page_entries if typ == "series"]
        movie_ids_on_page = [tid for typ, tid, _ in page_entries if typ == "movie"]
        has_unknown_on_page = any(typ == "unknown" for typ, _, _ in page_entries)

        resource_by_series: dict[str, list[FileResource]] = {}
        resource_by_movie: dict[str, list[FileResource]] = {}
        unknown_resources: list[FileResource] = []

        if series_ids_on_page:
            rs = (await db.execute(
                base_q.options(selectinload(FileResource.series), selectinload(FileResource.movie))
                .where(FileResource.series_id.in_(series_ids_on_page))
                .order_by(FileResource.published_at.desc())
            )).scalars().all()
            for r in rs:
                resource_by_series.setdefault(r.series_id, []).append(r)
        if movie_ids_on_page:
            rs = (await db.execute(
                base_q.options(selectinload(FileResource.series), selectinload(FileResource.movie))
                .where(FileResource.movie_id.in_(movie_ids_on_page))
                .order_by(FileResource.published_at.desc())
            )).scalars().all()
            for r in rs:
                resource_by_movie.setdefault(r.movie_id, []).append(r)
        if has_unknown_on_page:
            unknown_resources = list((await db.execute(
                base_q.options(selectinload(FileResource.series), selectinload(FileResource.movie))
                .where(FileResource.series_id.is_(None), FileResource.movie_id.is_(None))
                .order_by(FileResource.published_at.desc())
            )).scalars().all())

        def _iso(ts) -> str | None:
            return ts.isoformat() if ts is not None else None

        out = []
        for typ, tid, last_ts in page_entries:
            if typ == "series":
                items = resource_by_series.get(tid, [])
                if not items:
                    continue
                s = items[0].series
                out.append({
                    "type": "series",
                    "id": tid,
                    "title": (s.title_cn or s.title_en or s.original_title or tid) if s else tid,
                    "poster_url": s.poster_url if s else None,
                    "last_update": _iso(last_ts),
                    "resources": [FileResourceResponse.model_validate(r).model_dump() for r in items],
                })
            elif typ == "movie":
                items = resource_by_movie.get(tid, [])
                if not items:
                    continue
                m = items[0].movie
                out.append({
                    "type": "movie",
                    "id": tid,
                    "title": (m.title_cn or m.title_en or m.original_title or tid) if m else tid,
                    "poster_url": m.poster_url if m else None,
                    "last_update": _iso(last_ts),
                    "resources": [FileResourceResponse.model_validate(r).model_dump() for r in items],
                })
            else:  # unknown
                out.append({
                    "type": "unknown",
                    "id": None,
                    "title": "未识别",
                    "poster_url": None,
                    "last_update": _iso(last_ts),
                    "resources": [FileResourceResponse.model_validate(r).model_dump() for r in unknown_resources],
                })

        return success_response(
            {"groups": out},
            meta={"total": total_groups, "page": page, "page_size": page_size},
        )

    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()
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


# ---------------------------------------------------------------------------
# Field values — powers autocomplete for Filter DSL eq/ne inputs
# ---------------------------------------------------------------------------

# Only columns we actually let users filter on via the DSL are allowed. Numeric
# fields don't offer autocomplete (values are unbounded), and the JSON list
# column ``subtitle_langs`` needs a per-dialect unnest so we handle it below.
_AUTOCOMPLETE_STRING_FIELDS = {
    "subtitle_group", "resolution", "source", "video_codec", "audio_codec",
    "subtitle_type", "container", "title_cn", "title_en", "search_title",
}
_AUTOCOMPLETE_LIST_FIELDS = {"subtitle_langs"}
_AUTOCOMPLETE_ALL = _AUTOCOMPLETE_STRING_FIELDS | _AUTOCOMPLETE_LIST_FIELDS


@router.get("/channels/{channel_id}/field-values")
async def list_channel_field_values(
    channel_id: str,
    field: str = Query(..., description="FileResource column to enumerate"),
    q: str = Query("", description="Case-insensitive prefix filter"),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Return the top-N distinct values of ``field`` for this channel.

    Powers the Filter DSL editor's autocomplete on ``eq/ne/contains/fuzzy``
    inputs so users can pick from real values in the feed while still typing
    anything they want. Whitelist-guarded to prevent leaking arbitrary
    columns. Ordered by frequency descending so the most common option (e.g.
    ``1080p``) surfaces first.
    """
    ch = await db.get(Channel, channel_id)
    if not ch:
        return JSONResponse(
            status_code=404,
            content={"success": False, "data": None,
                     "error": {"code": "NOT_FOUND", "message": "Channel not found"}},
        )
    if field not in _AUTOCOMPLETE_ALL:
        return JSONResponse(
            status_code=422,
            content={"success": False, "data": None,
                     "error": {"code": "VALIDATION_ERROR",
                               "message": f"unsupported field {field!r}"}},
        )

    prefix = (q or "").strip().lower()

    if field in _AUTOCOMPLETE_STRING_FIELDS:
        col = getattr(FileResource, field)
        stmt = (
            select(col, func.count().label("cnt"))
            .where(
                FileResource.channel_id == channel_id,
                col.isnot(None),
                col != "",
            )
        )
        if prefix:
            stmt = stmt.where(func.lower(col).like(f"{prefix}%"))
        stmt = stmt.group_by(col).order_by(func.count().desc()).limit(limit)
        rows = (await db.execute(stmt)).all()
        return success_response([r[0] for r in rows])

    # subtitle_langs — JSON array column. SQLite and PostgreSQL disagree on
    # how to unnest, so pull the JSON blobs and aggregate in Python. The
    # per-channel volume is small (thousands at most), so a fetch-and-count
    # in memory is fine and avoids dialect-specific SQL.
    stmt = select(FileResource.subtitle_langs).where(
        FileResource.channel_id == channel_id,
        FileResource.subtitle_langs.isnot(None),
    )
    rows = (await db.execute(stmt)).all()
    counter: Counter[str] = Counter()
    for (val,) in rows:
        if not val:
            continue
        for tag in val:
            if not isinstance(tag, str):
                continue
            t = tag.strip()
            if not t:
                continue
            if prefix and not t.lower().startswith(prefix):
                continue
            counter[t] += 1
    top = [tag for tag, _cnt in counter.most_common(limit)]
    return success_response(top)


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
        results = await manual_search_metadata(
            db,
            body.search_title,
            body.content_type,
            body.data_source_type,
        )
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"success": False, "data": None, "error": {"code": "LLM_ERROR", "message": str(e)}},
        )
    return success_response({"results": [MetadataSearchResult(**r).model_dump() for r in results]})


@router.api_route("/resources/{resource_id}/metadata/link", methods=["POST", "PUT"])
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
