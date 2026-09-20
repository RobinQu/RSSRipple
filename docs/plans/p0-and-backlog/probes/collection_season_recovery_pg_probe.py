"""Rehearse conflict preflight, prior-version detach and indexed startup."""

import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host in {"127.0.0.1", "localhost"}
assert (url.username, url.password, url.database) == ("organize_test",) * 3
from sqlalchemy import select, text  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.models.channel import Channel  # noqa: E402
from app.models.episode import Episode  # noqa: E402
from app.models.file_resource import FileResource  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from app.models.work_external_id import WorkExternalId  # noqa: E402


async def child(*args, cwd=None):
    process = await asyncio.create_subprocess_exec(
        sys.executable, *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(process.communicate(), 20)
    return process.returncode, out.decode(), err.decode()


async def main():
    try:
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)
            await conn.run_sync(database.Base.metadata.create_all)
            await conn.execute(text("DROP INDEX uq_tv_series_collection_season"))
        async with database.async_session_factory() as db:
            parent = WorkCollection(title_cn="Synthetic old conflicting collection")
            channel = Channel(
                name="Synthetic recovery",
                url="https://example.invalid/rss",
                field_mapping={},
                metadata_agent_enabled=False,
            )
            db.add_all([parent, channel])
            await db.flush()
            works = [
                TVSeries(
                    title_cn=f"Synthetic protected work {i}",
                    collection_id=parent.id,
                    season_number=1,
                    manually_edited_fields=["title_cn"],
                )
                for i in range(2)
            ]
            db.add_all(works)
            await db.flush()
            episodes = [
                Episode(series_id=w.id, season=1, episode=1, title=f"Synthetic episode {i}")
                for i, w in enumerate(works)
            ]
            bags = [
                WorkExternalId(work_type="series", work_id=w.id, source="bangumi", external_id=f"bangumi:{900050 + i}")
                for i, w in enumerate(works)
            ]
            resources = [
                FileResource(
                    channel_id=channel.id,
                    guid=f"synthetic-recovery-{i}",
                    title_raw=f"Synthetic recovery {i}",
                    torrent_url=f"https://example.invalid/{i}.torrent",
                    series_id=w.id,
                    collection_id=parent.id,
                    season=1,
                    episode=1,
                )
                for i, w in enumerate(works)
            ]
            db.add_all([*episodes, *bags, *resources])
            await db.commit()
            parent_id = parent.id
            work_ids, episode_ids, bag_ids, resource_ids = (
                [r.id for r in rows] for rows in (works, episodes, bags, resources)
            )
        report = "/tmp/rssripple-v8-recovery-conflicts.jsonl"
        before = await child("-m", "scripts.verify_season_split", "--collection-conflicts-jsonl", report)
        assert before[0] == 1, before
        assert len(Path(report).read_text().splitlines()) == 2
        # Use the separate V7 app, which supports non-orphaning detach but
        # has no new uniqueness migration; do not start the conflicted V8 app.
        code = """import asyncio,sys
from pathlib import Path
import app.database as database
from app.api.v1 import collections
assert Path(collections.__file__).resolve().is_relative_to(Path.cwd())
async def run():
    try:
        async with database.async_session_factory() as db:
            response=await collections.detach_work(sys.argv[1],sys.argv[2],work_type='series',db=db)
            assert getattr(response,'status_code',200)==200
            await db.commit()
    finally:
        await database.engine.dispose()
asyncio.run(run())
"""
        detached = await child("-c", code, parent_id, work_ids[1], cwd="/tmp/rssripple-v7-collection-work")
        assert detached[0] == 0, detached
        after = await child("-m", "scripts.verify_season_split", "--collection-conflicts-jsonl", report)
        assert after[0] == 0 and not Path(report).read_text(), after
        await database.create_tables()
        await database.create_tables()
        async with database.async_session_factory() as db:
            restored = [await db.get(TVSeries, i) for i in work_ids]
            assert len(list(await db.scalars(select(TVSeries)))) == 2
            assert restored[0].collection_id == parent_id
            assert restored[1].collection_id and restored[1].collection_id != parent_id
            for i, w in enumerate(restored):
                assert (w.title_cn, w.season_number, w.manually_edited_fields) == (
                    f"Synthetic protected work {i}",
                    1,
                    ["title_cn"],
                )
                assert (await db.get(Episode, episode_ids[i])).series_id == w.id
                assert (await db.get(WorkExternalId, bag_ids[i])).work_id == w.id
                resource = await db.get(FileResource, resource_ids[i])
                assert (resource.series_id, resource.collection_id) == (w.id, w.collection_id)
            index = await db.scalar(
                text("SELECT indexname FROM pg_indexes WHERE indexname='uq_tv_series_collection_season'")
            )
            assert index
        print(
            json.dumps(
                {
                    "fixture": "synthetic protected works; actual PostgreSQL, CLI, V7 detach endpoint and V8 startup",
                    "preflight_before": before[0],
                    "prior_version_detach": detached[0],
                    "preflight_after": after[0],
                    "two_startups_passed": True,
                    "index_present": True,
                    "works_manual_fields_episodes_identities_resources_preserved": True,
                },
                indent=2,
            )
        )
    finally:
        await database.engine.dispose()


asyncio.run(main())
