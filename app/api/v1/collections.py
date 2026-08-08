"""WorkCollection API routes — franchise grouping (CRUD-lite).

One work belongs to at most one collection, enforced by the single nullable
``collection_id`` FK on TVSeries/Movie: attaching a work that is already in
another collection returns 409 DUPLICATE_SUBMISSION instead of moving it.
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.schemas.common import paginated_response, success_response
from app.schemas.work_collection import (
    CollectionPart,
    WorkCollectionAttach,
    WorkCollectionCreate,
    WorkCollectionResponse,
    WorkCollectionUpdate,
)
from app.services.collection_service import (
    TMDB_COLLECTION_SOURCE,
    collection_work_summaries,
    fetch_tmdb_collection_parts,
    filter_untracked_parts,
    tracked_movie_tmdb_ids,
)

router = APIRouter()


def _not_found() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "success": False,
            "data": None,
            "error": {"code": "NOT_FOUND", "message": "Collection not found"},
        },
    )


def _work_model(work_type: str):
    if work_type == "series":
        return TVSeries
    if work_type == "movie":
        return Movie
    return None


@router.get("/collections")
async def list_collections(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="Name fuzzy search"),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    base_q = select(WorkCollection)
    if search:
        pattern = f"%{search}%"
        base_q = base_q.where(
            or_(
                WorkCollection.title_cn.ilike(pattern),
                WorkCollection.title_en.ilike(pattern),
            )
        )
    total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_q.scalar_one()
    result = await db.execute(
        base_q.order_by(WorkCollection.created_at.desc()).offset(offset).limit(page_size)
    )
    items = result.scalars().all()
    # Member-work counts for the list rows (series + movie per collection).
    counts: dict[str, int] = {}
    if items:
        ids = [c.id for c in items]
        for model in (TVSeries, Movie):
            rows = await db.execute(
                select(model.collection_id, func.count())
                .where(model.collection_id.in_(ids))
                .group_by(model.collection_id)
            )
            for cid, cnt in rows.all():
                if cid is not None:
                    counts[cid] = counts.get(cid, 0) + cnt
    payload = []
    for c in items:
        d = WorkCollectionResponse.model_validate(c).model_dump()
        d["work_count"] = counts.get(c.id, 0)
        payload.append(d)
    return paginated_response(payload, total=total, page=page, page_size=page_size)


@router.post("/collections", status_code=201)
async def create_collection(
    body: WorkCollectionCreate,
    db: AsyncSession = Depends(get_db),
):
    collection = WorkCollection(**body.model_dump())
    db.add(collection)
    await db.flush()
    await db.refresh(collection)
    return success_response(WorkCollectionResponse.model_validate(collection).model_dump())


@router.get("/collections/{collection_id}")
async def get_collection(
    collection_id: str,
    include_parts: bool = Query(
        False, description="Also fetch TMDB collection parts (on demand, not persisted)"
    ),
    db: AsyncSession = Depends(get_db),
):
    collection = await db.get(WorkCollection, collection_id)
    if not collection:
        return _not_found()
    data = WorkCollectionResponse.model_validate(collection).model_dump()
    data["works"] = await collection_work_summaries(db, collection_id)
    if include_parts and collection.external_source == TMDB_COLLECTION_SOURCE:
        parts = await fetch_tmdb_collection_parts(collection)
        if parts is None:
            data["untracked_parts"] = []
        else:
            tracked = await tracked_movie_tmdb_ids(db)
            data["untracked_parts"] = [
                CollectionPart(**p).model_dump()
                for p in filter_untracked_parts(parts, tracked)
            ]
    return success_response(data)


@router.patch("/collections/{collection_id}")
async def update_collection(
    collection_id: str,
    body: WorkCollectionUpdate,
    db: AsyncSession = Depends(get_db),
):
    collection = await db.get(WorkCollection, collection_id)
    if not collection:
        return _not_found()
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(collection, key, value)
    await db.flush()
    await db.refresh(collection)
    return success_response(WorkCollectionResponse.model_validate(collection).model_dump())


@router.delete("/collections/{collection_id}")
async def delete_collection(collection_id: str, db: AsyncSession = Depends(get_db)):
    collection = await db.get(WorkCollection, collection_id)
    if not collection:
        return _not_found()
    # Detach member works (set NULL), then delete the collection itself.
    await db.execute(
        sql_update(TVSeries)
        .where(TVSeries.collection_id == collection_id)
        .values(collection_id=None)
    )
    await db.execute(
        sql_update(Movie)
        .where(Movie.collection_id == collection_id)
        .values(collection_id=None)
    )
    await db.delete(collection)
    await db.commit()
    return success_response({"deleted": True})


@router.post("/collections/{collection_id}/works", status_code=201)
async def attach_work(
    collection_id: str,
    body: WorkCollectionAttach,
    db: AsyncSession = Depends(get_db),
):
    collection = await db.get(WorkCollection, collection_id)
    if not collection:
        return _not_found()
    model = _work_model(body.work_type)
    if model is None:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "work_type must be 'series' or 'movie'",
                },
            },
        )
    work = await db.get(model, body.work_id)
    if work is None:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Work not found"},
            },
        )
    if work.collection_id == collection_id:
        # Already a member of THIS collection — idempotent attach.
        return success_response({"attached": True, "work_type": body.work_type, "work_id": body.work_id})
    if work.collection_id is not None:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "DUPLICATE_SUBMISSION",
                    "message": (
                        "Work already belongs to another collection "
                        f"({work.collection_id}); detach it first."
                    ),
                },
            },
        )
    work.collection_id = collection_id
    await db.flush()
    return success_response({"attached": True, "work_type": body.work_type, "work_id": body.work_id})


@router.delete("/collections/{collection_id}/works/{work_id}")
async def detach_work(
    collection_id: str,
    work_id: str,
    work_type: str = Query(..., description="'series' or 'movie'"),
    db: AsyncSession = Depends(get_db),
):
    collection = await db.get(WorkCollection, collection_id)
    if not collection:
        return _not_found()
    model = _work_model(work_type)
    if model is None:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "work_type must be 'series' or 'movie'",
                },
            },
        )
    work = await db.get(model, work_id)
    if work is None or work.collection_id != collection_id:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Work not found in this collection"},
            },
        )
    work.collection_id = None
    await db.flush()
    return success_response({"detached": True, "work_type": work_type, "work_id": work_id})
