"""Disposable PostgreSQL verification of concurrent orphan repair and lifecycle."""

import asyncio
import json
import os
import tempfile
import uuid

from sqlalchemy.engine import make_url

pg_url = os.environ["COLLECTION_PROBE_DATABASE_URL"]
url = make_url(pg_url)
assert url.drivername == "postgresql+asyncpg"
assert url.host in {"127.0.0.1", "localhost"}
assert (url.username, url.password, url.database) == ("organize_test",) * 3
os.environ["DATABASE_URL"] = pg_url
os.environ["POSTER_CACHE_DIR"] = tempfile.mkdtemp(prefix="rssripple-v7-posters-")

from sqlalchemy import event, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.models.channel import Channel  # noqa: E402
from app.models.file_resource import FileResource  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from app.models.work_external_id import WorkExternalId  # noqa: E402
from app.services import collection_lifecycle as lifecycle  # noqa: E402


async def main():
    engine = create_async_engine(pg_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database.engine, database.async_session_factory = engine, factory
    original = lifecycle.rehome_series
    first_locked, second_query = asyncio.Event(), asyncio.Event()
    queries = 0

    def observe(conn, cursor, statement, parameters, context, executemany):
        nonlocal queries
        if "FOR UPDATE" in statement and "collection_id IS NULL" in statement:
            queries += 1
            if queries == 2:
                second_query.set()

    async def hold_first(db, member):
        if not first_locked.is_set():
            first_locked.set()
            await asyncio.wait_for(second_query.wait(), 10)
        await original(db, member)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)
            await conn.run_sync(database.Base.metadata.create_all)
        identities = [str(uuid.uuid4()) for _ in range(3)]
        async with factory() as db:
            channel = Channel(
                name="Synthetic migration channel",
                url="https://example.invalid/rss",
                field_mapping={"list_locator": {"source": "entries"}, "field_mappings": {}},
                metadata_agent_enabled=False,
            )
            db.add(channel)
            await db.flush()
            for number, identity in enumerate(identities, 1):
                db.add(TVSeries(id=identity, title_en=f"Synthetic orphan {number}", season_number=number))
            await db.flush()
            for number, identity in enumerate(identities, 1):
                db.add(
                    FileResource(
                        channel_id=channel.id,
                        guid=str(uuid.uuid4()),
                        title_raw=f"Synthetic S0{number}E01",
                        torrent_url="https://example.invalid/test.torrent",
                        series_id=identity,
                        season=number,
                        episode=1,
                    )
                )
            await db.commit()
        lifecycle.rehome_series = hold_first
        event.listen(engine.sync_engine, "before_cursor_execute", observe)
        first = asyncio.create_task(lifecycle.backfill_orphan_collections())
        await asyncio.wait_for(first_locked.wait(), 10)
        second = asyncio.create_task(lifecycle.backfill_orphan_collections())
        counts = await asyncio.wait_for(asyncio.gather(first, second), 30)
        assert counts == [3, 0], counts
        lifecycle.rehome_series = original
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
        assert await lifecycle.backfill_orphan_collections() == 0
        async with factory() as db:
            members = list(await db.scalars(select(TVSeries)))
            assert len({row.collection_id for row in members}) == 3
            assert all(row.collection_id is not None for row in members)
            assert len(list(await db.scalars(select(WorkCollection)))) == 3
            by_id = {row.id: row for row in members}
            for resource in await db.scalars(select(FileResource)):
                assert resource.collection_id == by_id[resource.series_id].collection_id
                assert resource.season == by_id[resource.series_id].season_number
            member = by_id[identities[0]]
            old_id = member.collection_id
            db.add(
                WorkExternalId(
                    work_type="collection", work_id=old_id, source="wikipedia", external_id="wikipedia:en:900007"
                )
            )
            await db.flush()
            loaded = await db.scalar(
                select(WorkCollection).where(WorkCollection.id == old_id).options(selectinload(WorkCollection.series))
            )
            assert loaded.series
            await lifecycle.remove_collection(db, loaded)
            await db.commit()
        async with factory() as db:
            assert await db.get(WorkCollection, old_id) is None
            assert not list(await db.scalars(select(WorkExternalId)))
            member = await db.get(TVSeries, identities[0])
            assert member.collection_id not in (None, old_id)
            resource = await db.scalar(select(FileResource).where(FileResource.series_id == member.id))
            assert resource.collection_id == member.collection_id
        print(
            json.dumps(
                {
                    "backend": "PostgreSQL",
                    "concurrent_repair_counts": counts,
                    "second_query_started_while_first_held_row_locks": second_query.is_set(),
                    "idempotent_rerun": True,
                    "resource_and_season_consistent": True,
                    "delete_rehomes_and_cleans_bag": True,
                    "preloaded_parent": True,
                    "fixture": "synthetic ORM rows; real PostgreSQL",
                }
            )
        )
    finally:
        lifecycle.rehome_series = original
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
