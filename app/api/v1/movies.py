"""Movie API routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.responses import JSONResponse

from app.database import get_db
from app.models.movie import Movie
from app.schemas.movie import MovieCreate, MovieUpdate, MovieResponse
from app.schemas.common import success_response, paginated_response

router = APIRouter()


@router.get("/movies")
async def list_movies(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    total_q = await db.execute(select(func.count()).select_from(Movie))
    total = total_q.scalar_one()
    result = await db.execute(
        select(Movie).order_by(Movie.created_at.desc()).offset(offset).limit(page_size)
    )
    items = result.scalars().all()
    return paginated_response(
        [MovieResponse.model_validate(m).model_dump() for m in items],
        total=total, page=page, page_size=page_size,
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
    movie = await db.get(Movie, movie_id)
    if not movie:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Movie not found"}})
    return success_response(MovieResponse.model_validate(movie).model_dump())


@router.put("/movies/{movie_id}")
async def update_movie(
    movie_id: str,
    body: MovieUpdate,
    db: AsyncSession = Depends(get_db),
):
    movie = await db.get(Movie, movie_id)
    if not movie:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Movie not found"}})
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(movie, key, value)
    await db.flush()
    await db.refresh(movie)
    return success_response(MovieResponse.model_validate(movie).model_dump())


@router.delete("/movies/{movie_id}")
async def delete_movie(movie_id: str, db: AsyncSession = Depends(get_db)):
    from app.models.file_resource import FileResource
    from app.models.agent_work import AgentWork
    from app.models.pending_decision import PendingDecision
    from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
    from sqlalchemy import update as sql_update
    from sqlalchemy import select
    movie = await db.get(Movie, movie_id)
    if not movie:
        return JSONResponse(status_code=404, content={"success": False, "data": None, "error": {"code": "NOT_FOUND", "message": "Movie not found"}})
    await db.execute(sql_update(FileResource).where(FileResource.movie_id == movie_id).values(movie_id=None))
    await db.execute(sql_update(PendingDecision).where(PendingDecision.movie_id == movie_id).values(movie_id=None))
    await db.execute(sql_update(ChannelRawTitleMapping).where(ChannelRawTitleMapping.movie_id == movie_id).values(movie_id=None))
    res = await db.execute(select(AgentWork).where(AgentWork.movie_id == movie_id))
    for w in res.scalars().all():
        await db.delete(w)
    await db.delete(movie)
    await db.commit()
    return success_response({"deleted": True})
