"""Transactional collection changes, preserving season and resource identities."""

from collections import defaultdict

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.external_ids import delete_external_ids_for_work


async def rehome_series(db: AsyncSession, series: TVSeries) -> None:
    from app.services.metadata_service import _create_series_collection

    shell = await _create_series_collection(
        db,
        {
            "title_cn": series.title_cn,
            "title_en": series.title_en,
            "original_title": series.original_title,
            "alt_titles": series.aliases,
            "poster_url": series.poster_url,
            "description": series.description,
        },
    )
    # Keep both the FK and loaded back-populated collections consistent.
    # Otherwise deleting a preloaded old collection nullifies this member again.
    series.collection = shell
    await db.flush()


async def reconcile_resources(
    db: AsyncSession,
    collection_id: str | None,
    series_ids: list[str],
    movie_ids: list[str],
    *,
    deleting: bool,
) -> None:
    """Recompute only affected pointers; keep manual work/file mappings intact.

    A resource spanning multiple new collections has no unique collection.
    Unassigned collection-only resources stay with an existing collection, or
    lose that pointer when it is deleted; no work/season is invented.
    """
    terms = [FileResource.series_id.in_(series_ids), FileResource.movie_id.in_(movie_ids)]
    for model in (ResourceWorkLink, ResourceFileAssignment):
        terms.append(
            FileResource.id.in_(
                select(model.resource_id).where(
                    or_(
                        model.series_id.in_(series_ids),
                        model.movie_id.in_(movie_ids),
                    )
                )
            )
        )
    if deleting:
        terms.append(FileResource.collection_id == collection_id)
    cursor = ""
    while True:
        resources = (
            await db.execute(
                select(
                    FileResource.id,
                    FileResource.series_id,
                    FileResource.movie_id,
                )
                .where(or_(*terms), FileResource.id > cursor)
                .order_by(FileResource.id)
                .limit(100)
            )
        ).all()
        if not resources:
            return
        ids = [row.id for row in resources]
        targets = defaultdict(set)
        for row in resources:
            if row.series_id:
                targets[row.id].add(("series", row.series_id))
            if row.movie_id:
                targets[row.id].add(("movie", row.movie_id))
        for model in (ResourceWorkLink, ResourceFileAssignment):
            rows = (
                await db.execute(
                    select(model.resource_id, model.series_id, model.movie_id).where(model.resource_id.in_(ids))
                )
            ).all()
            for row in rows:
                if row.series_id:
                    targets[row.resource_id].add(("series", row.series_id))
                if row.movie_id:
                    targets[row.resource_id].add(("movie", row.movie_id))
        works = set().union(*targets.values()) if targets else set()
        collections = {}
        for kind, model in [("series", TVSeries), ("movie", Movie)]:
            work_ids = [identity for work_kind, identity in works if work_kind == kind]
            for identity, parent in (
                await db.execute(select(model.id, model.collection_id).where(model.id.in_(work_ids)))
            ).all():
                collections[(kind, identity)] = parent
        updates = defaultdict(list)
        for resource_id in ids:
            if not targets[resource_id] and not deleting:
                continue
            parents = {collections.get(work) for work in targets[resource_id]}
            parent = next(iter(parents)) if len(parents) == 1 else None
            updates[parent].append(resource_id)
        for parent, resource_ids in updates.items():
            await db.execute(update(FileResource).where(FileResource.id.in_(resource_ids)).values(collection_id=parent))
        cursor = ids[-1]


async def remove_collection(db: AsyncSession, collection: WorkCollection) -> None:
    members = (await db.scalars(select(TVSeries).where(TVSeries.collection_id == collection.id))).all()
    series_ids = [member.id for member in members]
    movie_ids = list(await db.scalars(select(Movie.id).where(Movie.collection_id == collection.id)))
    for member in members:
        await rehome_series(db, member)
    await db.execute(update(Movie).where(Movie.collection_id == collection.id).values(collection_id=None))
    await reconcile_resources(db, collection.id, series_ids, movie_ids, deleting=True)
    await delete_external_ids_for_work(db, "collection", collection.id)
    await db.delete(collection)
    await db.flush()


async def detach_member(db: AsyncSession, collection_id: str, work: TVSeries | Movie) -> None:
    is_series = isinstance(work, TVSeries)
    if is_series:
        await rehome_series(db, work)
    else:
        work.collection_id = None
        await db.flush()
    await reconcile_resources(
        db, collection_id, [work.id] if is_series else [], [] if is_series else [work.id], deleting=False
    )


async def repair_orphan_batch(db: AsyncSession) -> int:
    """Lock one bounded batch; the caller owns commit/rollback."""
    members = list(
        await db.scalars(
            select(TVSeries).where(TVSeries.collection_id.is_(None)).order_by(TVSeries.id).limit(100).with_for_update()
        )
    )
    for member in members:
        await rehome_series(db, member)
    if members:
        await reconcile_resources(db, None, [member.id for member in members], [], deleting=False)
    return len(members)


async def backfill_orphan_collections() -> int:
    from app import database

    async def repair_one_batch():
        async with database.async_session_factory() as db:
            count = await repair_orphan_batch(db)
            await db.commit()
            return count

    total = 0
    while count := await database.retry_on_lock(repair_one_batch):
        total += count
    return total
