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

import app.services.work_deletion as deletion
from sqlalchemy.exc import OperationalError

import app.database as database
import app.models  # noqa: F401
from app.api.v1.movies import delete_movie
from app.models.movie import Movie
from app.services.metadata_dedup import DedupReport, _merge_movie_group


async def case(merge_first):
    async with database.async_session_factory() as db:
        source, target = Movie(title_cn="Synthetic source"), Movie(title_cn="Synthetic target")
        db.add_all([source, target])
        await db.commit()
        sid, tid = source.id, target.id
    async with database.async_session_factory() as merger, database.async_session_factory() as deleter:
        source, target = await merger.get(Movie, sid), await merger.get(Movie, tid)
        if merge_first:
            await _merge_movie_group(merger, [target, source], DedupReport(), survivor=target)
            response = await asyncio.wait_for(delete_movie(sid, deleter), 5)
            assert response.status_code == 409
            await merger.commit()
            response = await delete_movie(sid, deleter)
            assert response.status_code == 404
        else:
            entered, release = asyncio.Event(), asyncio.Event()
            original = deletion.manual_deletion_references

            async def paused(*args):
                refs = await original(*args)
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
                return refs

            with patch.object(deletion, "manual_deletion_references", paused):
                task = asyncio.create_task(delete_movie(sid, deleter))
                await asyncio.wait_for(entered.wait(), 5)
                try:
                    await _merge_movie_group(merger, [target, source], DedupReport(), survivor=target)
                except OperationalError as exc:
                    assert "concurrent decision identity change" in str(exc)
                    await merger.rollback()
                else:
                    raise AssertionError("merge bypassed deletion identity coordination")
                release.set()
                await asyncio.wait_for(task, 5)
    async with database.async_session_factory() as db:
        assert await db.get(Movie, sid) is None
        assert await db.get(Movie, tid) is not None
    return {"merge_first": merge_first, "source_absent": True, "survivor_retained": True}


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    results = [await case(order) for order in (True, False)]
    Path("/tmp/rssripple-v12-merge-hw.json").write_text(json.dumps(results, indent=2) + "\n")
    print(results)
    await database.engine.dispose()


asyncio.run(main())
