"""Movie API routes."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.responses import error_response
from app.api.v1.title_listing import paginated_title_search
from app.database import get_db
from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.schemas.common import success_response
from app.schemas.movie import MovieCreate, MovieResponse, MovieUpdate
from app.services import fts as fts_service
from app.services.external_ids import add_external_id
from app.services.metadata_service import mark_manually_edited
from app.services.work_deletion import delete_work_with_guards

router = APIRouter()


@router.get("/movies")
async def list_movies(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="Title fuzzy search"),
    db: AsyncSession = Depends(get_db),
):
    return await paginated_title_search(
        db,
        Movie,
        fts_service.search_movie_fts,
        search,
        page=page,
        page_size=page_size,
        response_model=MovieResponse,
    )


@router.post("/movies", status_code=201)
async def create_movie(
    body: MovieCreate,
    db: AsyncSession = Depends(get_db),
):
    movie = Movie(**body.model_dump())
    db.add(movie)
    await db.flush()
    await db.refresh(movie)
    return success_response(MovieResponse.model_validate(movie).model_dump())


@router.get("/movies/{movie_id}")
async def get_movie(movie_id: str, db: AsyncSession = Depends(get_db)):
    from app.schemas.file_resource import FileResourceResponse

    movie = await db.get(Movie, movie_id)
    if not movie:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Movie not found"},
            },
        )
    data = MovieResponse.model_validate(movie).model_dump()

    # External-identity display links (registry-driven; replaces the old
    # client-side sourceLinks.ts computation). P3: the identity bag's
    # secondary ids are included, deduped against the primary external_id.
    from app.services.external_ids import list_external_ids
    from app.services.metadata_source_registry import build_source_links
    bag_ids = [
        r.external_id
        for r in await list_external_ids(db, "movie", movie.id)
        if r.external_id != movie.external_id
    ]
    data["source_links"] = build_source_links(
        movie.external_id, movie.external_source, "movie",
        wikipedia_url=movie.wikipedia_url,
        extra_ids=bag_ids,
    )

    # Resources
    res_q = await db.execute(
        select(FileResource)
        .where(FileResource.movie_id == movie_id)
        .options(
            selectinload(FileResource.audio_work),
            selectinload(FileResource.collection),
        )
        .order_by(FileResource.published_at.desc())
        .limit(20)
    )
    resources = res_q.scalars().all()
    data["resources"] = [FileResourceResponse.model_validate(r).model_dump() for r in resources]
    data["resource_count"] = len(resources)

    # Download tasks count
    task_cnt = await db.execute(
        select(func.count()).select_from(DownloadTask).where(
            DownloadTask.file_resource.has(FileResource.movie_id == movie_id)
        )
    )
    data["task_count"] = task_cnt.scalar_one() or 0

    # Agent works referencing this movie
    from app.models.agent_work import AgentWork
    aw_q = await db.execute(
        select(AgentWork).where(AgentWork.movie_id == movie_id)
    )
    data["agent_work_count"] = len(aw_q.scalars().all())

    # Collection summary + siblings (franchise grouping)
    data["collection"] = None
    data["collection_siblings"] = []
    if movie.collection_id:
        from app.models.work_collection import WorkCollection
        from app.services.collection_service import collection_work_summaries
        collection = await db.get(WorkCollection, movie.collection_id)
        if collection:
            data["collection"] = {
                "id": collection.id,
                "name": collection.title_cn or collection.title_en,
            }
            data["collection_siblings"] = await collection_work_summaries(
                db, collection.id, exclude=("movie", movie_id)
            )

    return success_response(data)


@router.put("/movies/{movie_id}")
async def update_movie(
    movie_id: str,
    body: MovieUpdate,
    db: AsyncSession = Depends(get_db),
):
    movie = await db.get(Movie, movie_id)
    if not movie:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "Movie not found"},
            },
        )
    update_data = body.model_dump(exclude_unset=True)
    # Manual identity change: keep the work reachable under its previous
    # (external_source, external_id) by bagging the old pair before the
    # primary columns are overwritten (idempotent; an id already owned by
    # another work is kept — only a warning is logged).
    if {"external_id", "external_source"} & update_data.keys():
        new_id = update_data.get("external_id", movie.external_id)
        new_source = update_data.get("external_source", movie.external_source)
        if (movie.external_source, movie.external_id) != (new_source, new_id):
            await add_external_id(
                db, "movie", movie.id, movie.external_source, movie.external_id
            )
    for key, value in update_data.items():
        setattr(movie, key, value)
    mark_manually_edited(movie, update_data)
    await db.flush()
    await db.refresh(movie)
    return success_response(MovieResponse.model_validate(movie).model_dump())


@router.delete("/movies/{movie_id}")
async def delete_movie(movie_id: str, db: AsyncSession = Depends(get_db)):
    outcome = await delete_work_with_guards(db, Movie, movie_id, kind="movie")
    if outcome.status == "busy":
        return error_response(
            409, "INVALID_STATE",
            "Work is being modified; retry deletion after the current operation finishes.",
        )
    if outcome.status == "not_found":
        return error_response(404, "NOT_FOUND", "Movie not found")
    if outcome.status == "references":
        return error_response(
            409, "DELETE_BLOCKED",
            "Reassign or remove manual resource mappings before deleting this work.",
            outcome.references,
        )
    if outcome.status == "agent_work":
        return error_response(
            409, "DELETE_BLOCKED",
            f"Cannot delete: {outcome.agent_work_count} agent(s) reference this movie. "
            "Remove the agent work subscriptions first.",
            {"agent_work_count": outcome.agent_work_count},
        )
    return success_response({"deleted": True})
