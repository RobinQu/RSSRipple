"""AudioWork API routes - non-TV/non-movie works (ASMR / music / drama CD / radio)."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.audio_work import AudioWork
from app.models.file_resource import FileResource
from app.schemas.audio_work import AudioWorkResponse, AudioWorkUpdate
from app.schemas.common import paginated_response, success_response
from app.services import fts as fts_service

router = APIRouter()


@router.get("/audio-works")
async def list_audio_works(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="Title fuzzy search"),
    content_type: str | None = Query(None, description="Filter by sub-kind"),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    if search:
        candidate_ids = await fts_service.search_audio_work_fts(db, search, limit=200)
        if candidate_ids:
            base_q = select(AudioWork).where(AudioWork.id.in_(candidate_ids))
            total = len(candidate_ids)
        else:
            pattern = f"%{search}%"
            base_q = select(AudioWork).where(
                or_(
                    AudioWork.title_cn.ilike(pattern),
                    AudioWork.title_en.ilike(pattern),
                    AudioWork.original_title.ilike(pattern),
                )
            )
            total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
            total = total_q.scalar_one()
    else:
        base_q = select(AudioWork)
        total_q = await db.execute(select(func.count()).select_from(base_q.subquery()))
        total = total_q.scalar_one()

    if content_type:
        base_q = base_q.where(AudioWork.content_type == content_type)

    result = await db.execute(
        base_q.order_by(AudioWork.created_at.desc()).offset(offset).limit(page_size)
    )
    items = result.scalars().all()
    return paginated_response(
        [AudioWorkResponse.model_validate(a).model_dump() for a in items],
        total=total, page=page, page_size=page_size,
    )


@router.get("/audio-works/{audio_work_id}")
async def get_audio_work(audio_work_id: str, db: AsyncSession = Depends(get_db)):
    from app.schemas.file_resource import FileResourceResponse

    audio = await db.get(AudioWork, audio_work_id)
    if not audio:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "AudioWork not found"},
            },
        )
    data = AudioWorkResponse.model_validate(audio).model_dump()

    res_q = await db.execute(
        select(FileResource)
        .where(FileResource.audio_work_id == audio_work_id)
        .order_by(FileResource.published_at.desc())
        .limit(20)
    )
    resources = res_q.scalars().all()
    data["resources"] = [FileResourceResponse.model_validate(r).model_dump() for r in resources]
    data["resource_count"] = len(resources)
    return success_response(data)


@router.put("/audio-works/{audio_work_id}")
async def update_audio_work(
    audio_work_id: str,
    body: AudioWorkUpdate,
    db: AsyncSession = Depends(get_db),
):
    audio = await db.get(AudioWork, audio_work_id)
    if not audio:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "AudioWork not found"},
            },
        )
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(audio, key, value)
    await db.flush()
    await fts_service.upsert_audio_work_fts(db, audio)
    await db.refresh(audio)
    return success_response(AudioWorkResponse.model_validate(audio).model_dump())


@router.delete("/audio-works/{audio_work_id}")
async def delete_audio_work(audio_work_id: str, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import update as sql_update

    audio = await db.get(AudioWork, audio_work_id)
    if not audio:
        return JSONResponse(
            status_code=404,
            content={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "AudioWork not found"},
            },
        )
    await db.execute(
        sql_update(FileResource)
        .where(FileResource.audio_work_id == audio_work_id)
        .values(audio_work_id=None)
    )
    await db.delete(audio)
    await fts_service.delete_audio_work_fts(db, audio_work_id)
    await db.commit()
    return success_response({"deleted": True})
