"""Shared resource↔work link helpers.

Several metadata pipelines add the same additive ``source="auto"`` link when
they resolve a resource to a work. Keeping one implementation guarantees the
exclusivity rule (``series_id`` for TV, ``movie_id`` for movies) and the
"never modify an existing row" contract stay identical everywhere.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.resource_work_link import ResourceWorkLink


async def ensure_auto_link(
    db: AsyncSession, resource_id: str, work_type: str, work_id: str
) -> bool:
    """Add a ``source="auto"`` resource↔work link unless one already exists.

    Additive only: existing rows (manual/llm/auto) are never modified. Returns
    ``True`` when a new row was added.
    """
    column = ResourceWorkLink.series_id if work_type == "series" else ResourceWorkLink.movie_id
    existing = (
        await db.execute(
            select(ResourceWorkLink.id).where(
                ResourceWorkLink.resource_id == resource_id,
                column == work_id,
            )
        )
    ).first()
    if existing:
        return False
    db.add(ResourceWorkLink(
        resource_id=resource_id,
        series_id=work_id if work_type == "series" else None,
        movie_id=work_id if work_type == "movie" else None,
        source="auto",
    ))
    return True
