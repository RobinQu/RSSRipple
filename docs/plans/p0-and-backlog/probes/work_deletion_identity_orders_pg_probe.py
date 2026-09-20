# ruff: noqa: E402
import asyncio
import os

from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
assert (url.username, url.password, url.database) == ("organize_test",) * 3
import json
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select, text

import app.database as database
import app.models  # noqa: F401
import app.services.external_ids as identities
from app.api.v1.movies import delete_movie
from app.api.v1.series import delete_series
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId


async def make_work(kind):
    async with database.async_session_factory() as db:
        if kind == "movie":
            work = Movie(title_cn="Synthetic lock-order movie")
        else:
            collection = WorkCollection(title_cn="Synthetic lock-order collection")
            db.add(collection)
            await db.flush()
            work = TVSeries(title_cn="Synthetic lock-order series", collection_id=collection.id, season_number=1)
        db.add(work)
        await db.commit()
        return work.id


async def case(kind, producer_first):
    wid = await make_work(kind)
    endpoint = delete_movie if kind == "movie" else delete_series
    external = wid.replace("-", "")
    async with database.async_session_factory() as producer, database.async_session_factory() as deleter:
        if producer_first:
            assert await identities.add_external_id(producer, kind, wid, "tmdb", external)
            response = await asyncio.wait_for(endpoint(wid, deleter), 5)
            assert response.status_code == 409
            assert json.loads(response.body)["error"]["code"] == "INVALID_STATE"
            await producer.commit()
            response = await endpoint(wid, deleter)
            assert response["success"]
        else:
            entered, release = asyncio.Event(), asyncio.Event()
            real_cleanup = identities.delete_external_ids_for_work

            async def pause_cleanup(*args):
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
                await real_cleanup(*args)

            pid = await producer.scalar(text("SELECT pg_backend_pid()"))
            with patch.object(identities, "delete_external_ids_for_work", pause_cleanup):
                deletion = asyncio.create_task(endpoint(wid, deleter))
                await asyncio.wait_for(entered.wait(), 5)
                registration = asyncio.create_task(identities.add_external_id(producer, kind, wid, "tmdb", external))
                async with database.async_session_factory() as observer:
                    for _ in range(100):
                        await observer.rollback()
                        waiting = await observer.scalar(
                            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}
                        )
                        if waiting == "Lock":
                            break
                        await asyncio.sleep(0.02)
                    assert waiting == "Lock", waiting
                release.set()
                await asyncio.wait_for(deletion, 5)
                assert await asyncio.wait_for(registration, 5) is False
                await producer.commit()
    async with database.async_session_factory() as db:
        model = Movie if kind == "movie" else TVSeries
        assert await db.get(model, wid) is None
        assert await db.scalar(select(WorkExternalId.id).where(WorkExternalId.work_id == wid)) is None
    return {"kind": kind, "producer_first": producer_first, "orphan": False}


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    results = [await case(kind, order) for kind in ("series", "movie") for order in (True, False)]
    Path("/tmp/rssripple-v12-deletion-gu.json").write_text(json.dumps(results, indent=2) + "\n")
    print(results)
    await database.engine.dispose()


asyncio.run(main())
