"""Race the actual attach API against an existing member's season update."""

import asyncio
import json
import os
from pathlib import Path

from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg"
assert url.host in {"127.0.0.1", "localhost"}
assert (url.username, url.password, url.database) == ("organize_test",) * 3

from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.api.v1 import collections  # noqa: E402
from app.main import unhandled_exception_handler  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from app.models.work_external_id import WorkExternalId  # noqa: E402


async def main():
    engine, factory = database.engine, database.async_session_factory
    ready, release = asyncio.Event(), asyncio.Event()
    original = collections.try_absorb_shell_collection

    async def pause_after_check(db, parent, work):
        ready.set()
        await asyncio.wait_for(release.wait(), 10)
        return await original(db, parent, work)

    async def get_db():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    api = FastAPI()
    api.include_router(collections.router, prefix="/api/v1")
    api.dependency_overrides[database.get_db] = get_db
    api.add_exception_handler(Exception, unhandled_exception_handler)
    task = None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)
            await conn.run_sync(database.Base.metadata.create_all)
        async with factory() as db:
            target = WorkCollection(title_cn="Synthetic target", aliases=["target alias"])
            shell = WorkCollection(title_cn="Synthetic shell", aliases=["shell alias"], external_source="series_group")
            db.add_all([target, shell])
            await db.flush()
            existing = TVSeries(title_cn="Synthetic existing S2", collection_id=target.id, season_number=2)
            incoming = TVSeries(title_cn="Synthetic incoming S1", collection_id=shell.id, season_number=1)
            bag = WorkExternalId(
                work_type="collection", work_id=shell.id, source="wikipedia", external_id="wikipedia:zh:900001"
            )
            db.add_all([existing, incoming, bag])
            await db.commit()
            keys = (target.id, shell.id, existing.id, incoming.id, bag.id)
        collections.try_absorb_shell_collection = pause_after_check
        async with AsyncClient(
            transport=ASGITransport(app=api, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            task = asyncio.create_task(
                client.post(f"/api/v1/collections/{keys[0]}/works", json={"work_type": "series", "work_id": keys[3]})
            )
            await asyncio.wait_for(ready.wait(), 10)
            # This write changes an existing member, so it needs neither a
            # parent FK check nor the parent lock held by the attach API.
            async with factory() as writer:
                await asyncio.wait_for(
                    writer.execute(update(TVSeries).where(TVSeries.id == keys[2]).values(season_number=1)), 10
                )
                await writer.commit()
            release.set()
            response = await asyncio.wait_for(task, 10)
        async with factory() as db:
            source = await db.get(WorkCollection, keys[1])
            target = await db.get(WorkCollection, keys[0])
            incoming = await db.get(TVSeries, keys[3])
            bag = await db.get(WorkExternalId, keys[4])
            result = {
                "fixture": "synthetic works; real PostgreSQL, production router and exception handler",
                "status": response.status_code,
                "error": response.json().get("error"),
                "source_shell_preserved": source is not None,
                "incoming_parent_preserved": incoming.collection_id == keys[1],
                "identity_preserved": bag.work_id == keys[1],
                "target_aliases_preserved": target.aliases == ["target alias"],
                "works_preserved": len(list(await db.scalars(select(TVSeries)))) == 2,
            }
            Path(os.environ["COLLECTION_API_PROBE_RESULT"]).write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
            assert response.status_code == 409, result
            assert response.json()["error"]["code"] == "DUPLICATE_SUBMISSION"
            assert all(
                result[k]
                for k in (
                    "source_shell_preserved",
                    "incoming_parent_preserved",
                    "identity_preserved",
                    "target_aliases_preserved",
                    "works_preserved",
                )
            )
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        collections.try_absorb_shell_collection = original
        await engine.dispose()


asyncio.run(main())
