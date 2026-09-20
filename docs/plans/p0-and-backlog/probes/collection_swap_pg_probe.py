"""Reverse shell moves through actual endpoints: lock order must not deadlock."""

import asyncio
import json
import os
import tempfile

from sqlalchemy.engine import make_url

pg_url = os.environ["COLLECTION_PROBE_DATABASE_URL"]
url = make_url(pg_url)
assert url.drivername == "postgresql+asyncpg"
assert url.host in {"127.0.0.1", "localhost"}
assert (url.username, url.password, url.database) == ("organize_test",) * 3
os.environ["DATABASE_URL"] = pg_url
os.environ["POSTER_CACHE_DIR"] = tempfile.mkdtemp(prefix="rssripple-v7-swap-")
ordered = os.environ.get("COLLECTION_PROBE_ORDERED_LOCKS") == "1"

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.api.v1 import collections as api  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from app.schemas.work_collection import WorkCollectionAttach  # noqa: E402


async def main():
    engine = create_async_engine(pg_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database.engine, database.async_session_factory = engine, factory
    entered = 0
    first_ready, both_ready, release, second_ready = [asyncio.Event() for _ in range(4)]
    original = api.try_absorb_shell_collection
    second_pid = None
    tasks = []

    async def pause(db, parent, member):
        nonlocal entered
        entered += 1
        first_ready.set()
        if ordered:
            await asyncio.wait_for(release.wait(), 10)
        else:
            if entered == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), 10)
        return await original(db, parent, member)

    async def move(target, identity, second=False):
        nonlocal second_pid
        async with factory() as db:
            if second:
                second_pid = await db.scalar(text("SELECT pg_backend_pid()"))
                second_ready.set()
            try:
                result = await api.attach_work(target, WorkCollectionAttach(work_type="series", work_id=identity), db)
                await db.commit()
                return {"status": getattr(result, "status_code", 201)}
            except DBAPIError as exc:
                await db.rollback()
                return {"status": 500, "sqlstate": getattr(exc.orig, "sqlstate", None)}

    try:
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)
            await conn.run_sync(database.Base.metadata.create_all)
        parents, members = [], []
        async with factory() as db:
            for number in (1, 2):
                parent = WorkCollection(title_cn=f"Synthetic shell {number}", external_source="series_group")
                db.add(parent)
                await db.flush()
                member = TVSeries(title_en=f"Synthetic season {number}", season_number=number, collection_id=parent.id)
                db.add(member)
                await db.flush()
                parents.append(parent.id)
                members.append(member.id)
            await db.commit()
        api.try_absorb_shell_collection = pause
        first = asyncio.create_task(move(parents[1], members[0]))
        tasks.append(first)
        await asyncio.wait_for(first_ready.wait(), 10)
        second = asyncio.create_task(move(parents[0], members[1], True))
        tasks.append(second)
        await asyncio.wait_for(second_ready.wait(), 10)
        blocked = False
        if ordered:
            async with engine.connect() as observer:
                for _ in range(40):
                    if await observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid}):
                        blocked = True
                        break
                    if second.done():
                        break
                    await asyncio.sleep(0.05)
            release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        async with factory() as db:
            remaining = list(await db.scalars(select(WorkCollection.id)))
            member_parents = list(await db.scalars(select(TVSeries.collection_id)))
        result = {
            "backend": "PostgreSQL",
            "ordered_probe": ordered,
            "results": results,
            "second_blocked": blocked,
            "remaining_collections": len(remaining),
            "members_preserved": len(member_parents) == 2 and all(p in remaining for p in member_parents),
        }
        print(json.dumps(result), flush=True)
        assert all(row["status"] != 500 for row in results), result
        assert [row["status"] for row in results] == [201, 404], result
        assert len(remaining) == 1 and result["members_preserved"], result
        if ordered:
            assert blocked, result
    finally:
        release.set()
        both_ready.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        api.try_absorb_shell_collection = original
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
