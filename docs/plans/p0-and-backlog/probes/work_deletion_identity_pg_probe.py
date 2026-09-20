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
from app.models.movie import Movie
from app.models.work_external_id import WorkExternalId


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    async with database.async_session_factory() as db:
        work = Movie(title_cn="Synthetic registration-delete race")
        db.add(work)
        await db.commit()
        wid = work.id
    cleaned = asyncio.Event()
    cleanup = identities.delete_external_ids_for_work

    async def observed_cleanup(*args):
        await cleanup(*args)
        cleaned.set()

    async with database.async_session_factory() as producer:
        assert await identities.add_external_id(producer, "movie", wid, "tmdb", "987654324")
        async with database.async_session_factory() as deleter:
            pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
            with patch.object(identities, "delete_external_ids_for_work", observed_cleanup):
                task = asyncio.create_task(delete_movie(wid, deleter))
                await asyncio.wait_for(cleaned.wait(), 5)
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
                await producer.commit()
                await asyncio.wait_for(task, 5)
    async with database.async_session_factory() as db:
        orphan = await db.scalar(select(WorkExternalId.id).where(WorkExternalId.work_id == wid))
        remaining = await db.get(Movie, wid)
        result = {
            "work_deleted": remaining is None,
            "orphan_identity_retained": orphan is not None,
            "delete_waited_on_real_pg_lock": True,
        }
        Path("/tmp/rssripple-v12-deletion-gr.json").write_text(json.dumps(result, indent=2) + "\n")
        print(result)
        assert remaining is None and orphan is None
    await database.engine.dispose()


asyncio.run(main())
