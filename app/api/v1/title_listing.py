"""Shared paginated title search for series/movie list endpoints.

Both routes expose identical behavior: FTS candidate retrieval with an ILIKE
fallback, newest-first ordering, and the standard paginated envelope. Keeping
one implementation ensures the two lists stay in lockstep.
"""

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.common import paginated_response


async def paginated_title_search(
    db: AsyncSession,
    model: Any,
    search_fts: Any,
    search: str | None,
    *,
    page: int,
    page_size: int,
    response_model: Any,
) -> dict:
    offset = (page - 1) * page_size
    if search:
        # Use FTS5 for CJK-aware full-text search
        candidate_ids = await search_fts(db, search, limit=200)
        if candidate_ids:
            base_q = select(model).where(model.id.in_(candidate_ids))
            total = len(candidate_ids)
        else:
            # Fallback to ILIKE if FTS5 returns nothing
            pattern = f"%{search}%"
            base_q = select(model).where(
                or_(
                    model.title_cn.ilike(pattern),
                    model.title_en.ilike(pattern),
                    model.original_title.ilike(pattern),
                )
            )
            total = (await db.execute(
                select(func.count()).select_from(base_q.subquery())
            )).scalar_one()
    else:
        base_q = select(model)
        total = (await db.execute(
            select(func.count()).select_from(base_q.subquery())
        )).scalar_one()

    result = await db.execute(
        base_q.order_by(model.created_at.desc()).offset(offset).limit(page_size)
    )
    items = result.scalars().all()
    return paginated_response(
        [response_model.model_validate(item).model_dump() for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )
