"""Controlled PostgreSQL race through the real collection deletion endpoint."""

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
os.environ["POSTER_CACHE_DIR"] = tempfile.mkdtemp(prefix="rssripple-v7-delete-")

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.api.v1.collections import delete_collection  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from app.services import collection_lifecycle as lifecycle  # noqa: E402


async def main():
    engine = create_async_engine(pg_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database.engine, database.async_session_factory = engine, factory
    ready, release, second_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = lifecycle.rehome_series
    second_pid = None
    tasks = []

    async def hold_first(db, member):
        if asyncio.current_task().get_name() == "first-delete":
            ready.set()
            await asyncio.wait_for(release.wait(), 10)
        await original(db, member)

    async def delete(name, identity):
        nonlocal second_pid
        async with factory() as db:
            if name == "second-delete":
                second_pid = await db.scalar(text("SELECT pg_backend_pid()"))
                second_ready.set()
            result = await delete_collection(identity, db)
            return getattr(result, "status_code", 200)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)
            await conn.run_sync(database.Base.metadata.create_all)
        async with factory() as db:
            parent = WorkCollection(title_cn="Synthetic Concurrent Deletion")
            db.add(parent)
            await db.flush()
            member = TVSeries(title_en="Synthetic Concurrent Season", collection_id=parent.id)
            db.add(member)
            await db.commit()
            identity, member_id = parent.id, member.id
        lifecycle.rehome_series = hold_first
        first = asyncio.create_task(delete("first-delete", identity), name="first-delete")
        tasks.append(first)
        await asyncio.wait_for(ready.wait(), 10)
        second = asyncio.create_task(delete("second-delete", identity), name="second-delete")
        tasks.append(second)
        await asyncio.wait_for(second_ready.wait(), 10)
        blocked = False
        async with engine.connect() as observer:
            for _ in range(40):
                blockers = await observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid})
                if blockers:
                    blocked = True
                    break
                if second.done():
                    break
                await asyncio.sleep(0.05)
        release.set()
        statuses = await asyncio.wait_for(asyncio.gather(first, second), 10)
        async with factory() as db:
            parents = list(await db.scalars(select(WorkCollection.id)))
            member = await db.get(TVSeries, member_id)
            result = {
                "backend": "PostgreSQL",
                "statuses": statuses,
                "second_blocked": blocked,
                "remaining_collections": len(parents),
                "member_has_valid_collection": member.collection_id in parents,
                "fixture": "synthetic rows; actual endpoint transactions and pg_blocking_pids",
            }
        print(json.dumps(result), flush=True)
        assert statuses == [200, 404], result
        assert blocked, result
        assert len(parents) == 1 and member.collection_id in parents, result
    finally:
        release.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        lifecycle.rehome_series = original
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
