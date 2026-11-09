"""Link existing season works explicitly declared by a multi-season resource."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.file_resource import FileResource


async def _confirmed_collection_id(
    db: AsyncSession, resource: FileResource
) -> str | None:
    work_ids = {resource.series_id} if resource.series_id else set()
    links = (await db.scalars(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource.id)
    )).all()
    work_ids.update(link.series_id for link in links if link.series_id)
    if not work_ids:
        return None
    works = (await db.scalars(
        select(TVSeries).where(TVSeries.id.in_(work_ids))
    )).all()
    if len(works) != len(work_ids):
        return None
    collection_ids = {work.collection_id for work in works}
    if len(collection_ids) != 1:
        return None
    return collection_ids.pop()


async def link_declared_seasons(db: AsyncSession, resource: FileResource) -> int:
    """Add auto links for unambiguous, existing works in declared seasons.

    The resource's existing flat/base work and work links must confirm one
    collection. No titles are matched and no works are created. Existing link
    rows, including manual ones, are left untouched; the flat FK remains
    available to the initial and later single-work file-binding passes.
    """
    if (
        not resource.is_batch
        or resource.batch_scope != "multi_season"
        or not resource.batch_seasons
    ):
        return 0
    declared = {
        season for season in resource.batch_seasons
        if isinstance(season, int) and not isinstance(season, bool)
    }
    if not declared:
        return 0
    collection_id = await _confirmed_collection_id(db, resource)
    if collection_id is None:
        return 0

    candidates = (await db.scalars(
        select(TVSeries).where(
            TVSeries.collection_id == collection_id,
            TVSeries.season_number.in_(declared),
        )
    )).all()
    by_season: dict[int, list[TVSeries]] = defaultdict(list)
    for work in candidates:
        by_season[work.season_number].append(work)
    existing = {
        link.series_id
        for link in (await db.scalars(
            select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource.id)
        )).all()
        if link.series_id
    }
    created = 0
    for season in sorted(declared):
        works = by_season.get(season, [])
        if len(works) != 1 or works[0].id in existing:
            continue
        db.add(ResourceWorkLink(
            resource_id=resource.id,
            series_id=works[0].id,
            source="auto",
        ))
        existing.add(works[0].id)
        created += 1

    from app.services.batch_content_analysis import sync_resource_collection

    await sync_resource_collection(db, resource)
    return created


__all__ = ["link_declared_seasons"]
