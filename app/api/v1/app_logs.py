"""Application error log browsing and manual external-dependency retries."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.app_log import AppLog
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.schemas.app_log import AppLogResource, AppLogResponse
from app.schemas.common import paginated_response, success_response
from app.services.app_logs import retry_external_failure

router = APIRouter()


@router.get("/app-logs")
async def list_app_logs(
    category: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    predicates = []
    if category:
        predicates.append(AppLog.category == category)
    if status:
        predicates.append(AppLog.status == status)
    total = int((await db.execute(
        select(func.count()).select_from(AppLog).where(*predicates)
    )).scalar_one() or 0)
    logs = list((await db.execute(
        select(AppLog)
        .where(*predicates)
        .order_by(AppLog.updated_at.desc(), AppLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars().all())
    resource_ids = {row.resource_id for row in logs if row.resource_id}
    channel_ids = {row.channel_id for row in logs if row.channel_id}
    resources = {
        row.id: row for row in (await db.execute(
            select(FileResource)
            .where(FileResource.id.in_(resource_ids))
            .options(
                selectinload(FileResource.series),
                selectinload(FileResource.movie),
                selectinload(FileResource.collection),
            )
        )).scalars().all()
    } if resource_ids else {}
    channels = {
        row.id: row for row in (await db.execute(
            select(Channel).where(Channel.id.in_(channel_ids))
        )).scalars().all()
    } if channel_ids else {}
    items = []
    for row in logs:
        resource = resources.get(row.resource_id)
        channel = channels.get(row.channel_id)
        work = (
            resource.series or resource.movie or resource.collection
            if resource is not None else None
        )
        resource_detail = AppLogResource(
            id=resource.id,
            title_raw=resource.title_raw,
            title_cn=resource.title_cn,
            title_en=resource.title_en,
            search_title=resource.search_title,
            guid=resource.guid,
            detail_url=resource.detail_url,
            work_title=(
                work.title_cn or work.title_en or getattr(work, "original_title", None)
                if work is not None else None
            ),
            series_id=resource.series_id,
            movie_id=resource.movie_id,
            collection_id=resource.collection_id,
            season=resource.season,
            episode=resource.episode,
            absolute_episode=resource.absolute_episode,
            is_batch=resource.is_batch,
            batch_scope=resource.batch_scope,
            metadata_attempts=resource.metadata_attempts,
            metadata_failure_type=resource.metadata_failure_type,
            torrent_fetch_status=resource.torrent_fetch_status,
            torrent_fetch_error=resource.torrent_fetch_error,
            magnet_resolve_status=resource.magnet_resolve_status,
            magnet_resolve_error=resource.magnet_resolve_error,
            listing_reconcile_status=resource.listing_reconcile_status,
            listing_reconcile_error=resource.listing_reconcile_error,
        ) if resource is not None else None
        items.append(AppLogResponse(
            id=row.id,
            category=row.category,
            severity=row.severity,
            message=row.message,
            resource_id=row.resource_id,
            channel_id=row.channel_id,
            resource_title=(
                resource.search_title or resource.title_cn or resource.title_en
                or resource.title_raw
            ) if resource is not None else None,
            resource=resource_detail,
            channel_name=channel.name if channel else None,
            attempts=row.attempts,
            status=row.status,
            retry_count=row.retry_count,
            last_retried_at=row.last_retried_at,
            resolved_at=row.resolved_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        ).model_dump())
    return paginated_response(items, total, page, page_size)


@router.post("/app-logs/{log_id}/retry")
async def retry_app_log(log_id: str, db: AsyncSession = Depends(get_db)):
    from app.services.app_logs import RetryAlreadyRunningError

    row = await db.get(AppLog, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Application log not found")
    try:
        await retry_external_failure(db, row)
    except RetryAlreadyRunningError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return success_response({"retry": {"status": "pending", "log_id": log_id}})
