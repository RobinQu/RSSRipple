"""Race two actual attach requests for one collection season slot."""

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
from sqlalchemy import select, text  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.api.v1 import collections  # noqa: E402
from app.main import unhandled_exception_handler  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from app.models.file_resource import FileResource  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from app.models.work_external_id import WorkExternalId  # noqa: E402


async def main():
    engine, factory = database.engine, database.async_session_factory
    ready, release, second_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = collections.try_absorb_shell_collection
    backend = {}
    tasks = []

    async def hold_first(db, parent, work):
        if work.id == work_ids[0]:
            ready.set()
            await asyncio.wait_for(release.wait(), 10)
        return await original(db, parent, work)

    async def get_db():
        async with factory() as db:
            try:
                if asyncio.current_task().get_name() == "second-api":
                    backend["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                    second_ready.set()
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    api = FastAPI()
    api.include_router(collections.router, prefix="/api/v1")
    api.dependency_overrides[database.get_db] = get_db
    api.add_exception_handler(Exception, unhandled_exception_handler)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)
            await conn.run_sync(database.Base.metadata.create_all)
        async with factory() as db:
            target = WorkCollection(title_cn="Synthetic shared target", aliases=["target"])
            shells = [
                WorkCollection(
                    title_cn=f"Synthetic source {i}", external_source="series_group", aliases=[f"source {i}"]
                )
                for i in range(2)
            ]
            channel = Channel(
                name="Synthetic pair probe",
                url="https://example.invalid/rss",
                field_mapping={},
                metadata_agent_enabled=False,
            )
            db.add_all([target, channel, *shells])
            await db.flush()
            works = [
                TVSeries(title_cn=f"Synthetic S1 candidate {i}", collection_id=shell.id, season_number=1)
                for i, shell in enumerate(shells)
            ]
            bags = [
                WorkExternalId(
                    work_type="collection",
                    work_id=shell.id,
                    source="wikipedia",
                    external_id=f"wikipedia:zh:{900010 + i}",
                )
                for i, shell in enumerate(shells)
            ]
            db.add_all([*works, *bags])
            await db.flush()
            resources = [
                FileResource(
                    channel_id=channel.id,
                    guid=f"synthetic-pair-{i}",
                    title_raw=f"Synthetic S01E01 {i}",
                    torrent_url=f"https://example.invalid/{i}.torrent",
                    series_id=work.id,
                    collection_id=shells[i].id,
                    season=1,
                    episode=1,
                )
                for i, work in enumerate(works)
            ]
            db.add_all(resources)
            await db.commit()
            target_id = target.id
            shell_ids, work_ids, bag_ids, resource_ids = (
                [r.id for r in rows] for rows in (shells, works, bags, resources)
            )
        collections.try_absorb_shell_collection = hold_first
        async with AsyncClient(
            transport=ASGITransport(app=api, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            first = asyncio.create_task(
                client.post(
                    f"/api/v1/collections/{target_id}/works", json={"work_type": "series", "work_id": work_ids[0]}
                ),
                name="first-api",
            )
            tasks.append(first)
            await asyncio.wait_for(ready.wait(), 10)
            second = asyncio.create_task(
                client.post(
                    f"/api/v1/collections/{target_id}/works", json={"work_type": "series", "work_id": work_ids[1]}
                ),
                name="second-api",
            )
            tasks.append(second)
            await asyncio.wait_for(second_ready.wait(), 10)

            async def observe_blocker():
                async with engine.connect() as conn:
                    while True:
                        if await conn.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": backend["pid"]}):
                            return True
                        await asyncio.sleep(0.05)

            blocked = await asyncio.wait_for(observe_blocker(), 10)
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(first, second), 15)
        async with factory() as db:
            members = list(await db.scalars(select(TVSeries).where(TVSeries.collection_id == target_id)))
            assert len(members) == 1 and members[0].id == work_ids[0]
            assert await db.get(WorkCollection, shell_ids[0]) is None
            assert await db.get(WorkCollection, shell_ids[1]) is not None
            assert (await db.get(TVSeries, work_ids[1])).collection_id == shell_ids[1]
            for i, parent in enumerate([target_id, shell_ids[1]]):
                assert (await db.get(WorkExternalId, bag_ids[i])).work_id == parent
                resource = await db.get(FileResource, resource_ids[i])
                assert (resource.collection_id, resource.series_id) == (parent, work_ids[i])
            assert (await db.get(WorkCollection, target_id)).aliases == ["target", "source 0"]
            result = {
                "fixture": "synthetic works; real PostgreSQL and production HTTP ASGI router",
                "statuses": [r.status_code for r in responses],
                "second_error": responses[1].json().get("error"),
                "second_backend_blocked": blocked,
                "one_target_member": True,
                "both_work_ids_preserved": True,
                "losing_shell_preserved": True,
                "identities_and_resources_follow_correct_parents": True,
                "target_aliases_from_winner_only": True,
            }
            Path(os.environ["COLLECTION_API_PROBE_RESULT"]).write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
            assert result["statuses"] == [201, 409]
            assert result["second_error"]["code"] == "DUPLICATE_SUBMISSION"
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        collections.try_absorb_shell_collection = original
        await engine.dispose()


asyncio.run(main())
