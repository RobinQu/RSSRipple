"""Synthetic PostgreSQL replacement races on a dedicated ephemeral database."""

import asyncio
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.database import Base
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.resource_publication import ResourcePublication
from app.services.agent_publication_progress import (
    acknowledge_publications,
    reset_progress,
    snapshot_publications,
)
from app.services.resource_publication import publish_resource


async def main():
    url = os.environ["PROBE_DATABASE_URL"]
    assert url.startswith("postgresql+asyncpg://organize_test:organize_test@127.0.0.1:")
    assert url.endswith("/organize_test")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cases = []
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        for first_commits in (False, True):
            async with factory() as db:
                channel = Channel(
                    name="Synthetic retention", url="https://example.invalid/" + str(uuid.uuid4()), field_mapping={}
                )
                downloader = DownloaderInstance(name="Synthetic", type="mock", url="mock://test", download_dir="/tmp")
                db.add_all([channel, downloader])
                await db.flush()
                agent = Agent(name="Synthetic", channel_id=channel.id, downloader_id=downloader.id)
                db.add(agent)
                await db.flush()
                await reset_progress(db, agent.id, channel.id)
                resource = FileResource(
                    channel_id=channel.id, guid=str(uuid.uuid4()), title_raw="Synthetic", torrent_url="synthetic"
                )
                db.add(resource)
                await db.flush()
                await publish_resource(db, resource.id, kind="created")
                await publish_resource(db, resource.id, kind="metadata")
                await db.commit()
                old_snapshot = await snapshot_publications(db, agent.id, channel.id)
                assert old_snapshot.through == 2
            async with factory() as first, factory() as second, factory() as observer:
                await publish_resource(first, resource.id, kind="metadata")
                pid = await second.scalar(text("select pg_backend_pid()"))

                async def replace_second():
                    event = await publish_resource(second, resource.id, kind="metadata")
                    await second.commit()
                    return event.sequence

                task = asyncio.create_task(replace_second())
                try:
                    async with asyncio.timeout(10):
                        while True:
                            await observer.rollback()
                            wait = await observer.scalar(
                                text("select wait_event_type from pg_stat_activity where pid=:pid"), {"pid": pid}
                            )
                            if wait == "Lock":
                                break
                            assert not task.done(), "replacement escaped transaction serialization"
                            await asyncio.sleep(0.02)
                    visible = list(
                        await observer.scalars(
                            select(ResourcePublication.sequence)
                            .where(ResourcePublication.resource_id == resource.id)
                            .order_by(ResourcePublication.sequence)
                        )
                    )
                    assert visible == [1, 2]
                    if first_commits:
                        await first.commit()
                    else:
                        await first.rollback()
                    latest = await asyncio.wait_for(task, 5)
                    assert latest == (4 if first_commits else 3)
                    await observer.rollback()
                    events = list(
                        await observer.scalars(
                            select(ResourcePublication)
                            .where(ResourcePublication.resource_id == resource.id)
                            .order_by(ResourcePublication.sequence)
                        )
                    )
                    assert [(e.kind, e.sequence, e.origin_sequence) for e in events] == [
                        ("created", 1, 1),
                        ("metadata", latest, 1),
                    ]
                    assert await acknowledge_publications(observer, old_snapshot)
                    await observer.commit()
                    new_snapshot = await snapshot_publications(observer, agent.id, channel.id)
                    assert new_snapshot.resource_ids == (resource.id,)
                    assert new_snapshot.through == latest
                    assert await acknowledge_publications(observer, new_snapshot)
                    await observer.commit()
                    assert not (await snapshot_publications(observer, agent.id, channel.id)).resource_ids
                    cases.append(
                        {
                            "first_commits": first_commits,
                            "lock_observed": True,
                            "visible_during_replace": visible,
                            "final_sequences": [1, latest],
                            "old_ack_preserved_new_work": True,
                            "event_count": len(events),
                        }
                    )
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
    finally:
        await engine.dispose()
    Path(os.environ["PROBE_RESULT_PATH"]).write_text(json.dumps({"data": "synthetic", "cases": cases}, indent=2) + "\n")


asyncio.run(main())
