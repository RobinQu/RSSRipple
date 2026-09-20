"""Concurrent production metadata upserts must converge on one season work."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host in {"127.0.0.1", "localhost"}
assert (url.username, url.password, url.database) == ("organize_test",) * 3
from sqlalchemy import select, text  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from app.models.work_external_id import WorkExternalId  # noqa: E402
from app.services import metadata_service as metadata  # noqa: E402


async def main():
    engine, factory = database.engine, database.async_session_factory
    original = metadata._create_season_work
    poster = metadata.download_and_cache_poster
    barrier = asyncio.Event()
    arrived = 0
    locked = os.environ.get("METADATA_PROBE_LOCKED") == "1"
    ready, release, second_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    backend = {}

    async def rendezvous(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if locked:
            ready.set()
            await asyncio.wait_for(release.wait(), 10)
            return await original(*args, **kwargs)
        if arrived == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 10)
        return await original(*args, **kwargs)

    async def upsert():
        async with factory() as db:
            try:
                if asyncio.current_task().get_name() == "second-upsert":
                    backend["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                    second_ready.set()
                work = await metadata.create_or_update_series_from_external(
                    db,
                    {
                        "external_source": "tmdb",
                        "external_id": "tmdb:900020",
                        "title_cn": "Synthetic concurrency",
                        "content_type": "tv",
                    },
                    season_hint=1,
                )
                await db.commit()
                return {"id": work.id}
            except Exception as exc:
                await db.rollback()
                return {"error": type(exc).__name__, "message": str(exc)}

    try:
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)
            await conn.run_sync(database.Base.metadata.create_all)
        async with factory() as db:
            parent = WorkCollection(title_cn="Synthetic concurrency", aliases=["Synthetic concurrency"])
            db.add(parent)
            await db.flush()
            db.add(WorkExternalId(work_type="collection", work_id=parent.id, source="tmdb", external_id="tmdb:900020"))
            await db.commit()
        metadata.download_and_cache_poster = AsyncMock(return_value=None)
        metadata._create_season_work = rendezvous
        if locked:
            first = asyncio.create_task(upsert(), name="first-upsert")
            await asyncio.wait_for(ready.wait(), 10)
            second = asyncio.create_task(upsert(), name="second-upsert")
            await asyncio.wait_for(second_ready.wait(), 10)

            async def observe_blocker():
                async with engine.connect() as conn:
                    while True:
                        if await conn.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": backend["pid"]}):
                            return True
                        await asyncio.sleep(0.05)

            await asyncio.wait_for(observe_blocker(), 10)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 20)
        else:
            results = await asyncio.wait_for(asyncio.gather(upsert(), upsert()), 20)
        async with factory() as db:
            rows = list(await db.scalars(select(TVSeries)))
            result = {
                "fixture": "synthetic source identity; real PostgreSQL and production upsert",
                "results": results,
                "work_count": len(rows),
                "second_backend_blocked": locked,
                "same_work": all("id" in r for r in results) and results[0]["id"] == results[1]["id"],
            }
            Path(os.environ["METADATA_PROBE_RESULT"]).write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
            assert result["same_work"] and len(rows) == 1, result
    finally:
        metadata._create_season_work = original
        metadata.download_and_cache_poster = poster
        await engine.dispose()


asyncio.run(main())
