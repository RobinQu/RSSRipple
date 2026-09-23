"""Dedicated PostgreSQL migration barrier and publisher handoff probe."""

import asyncio
import json
import os
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.database import Base
from app.models.app_setting import AppSetting
from app.models.file_resource import FileResource
from app.models.resource_publication import ResourcePublication
from app.services.agent_publication_progress import snapshot_publications
from app.services.publication_migration import MARKER, bootstrap_publications
from app.services.resource_publication import publish_resource
from tests.unit.test_publication_migration import legacy


async def main():
    url = os.environ["PROBE_DATABASE_URL"]
    assert url.startswith("postgresql+asyncpg://organize_test:organize_test@127.0.0.1:")
    assert url.endswith("/organize_test")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as db:
        agent, old, rows = await legacy(db)
        await db.commit()
        aid, cid, eligible = agent.id, old.channel_id, rows[1].id
    async with factory() as db:
        await bootstrap_publications(db, writers_stopped=True)
        await db.rollback()
    async with factory() as observer:
        assert await observer.get(AppSetting, MARKER) is None
        assert await observer.scalar(select(ResourcePublication.id)) is None
    async with factory() as migration, factory() as publisher, factory() as observer:
        result = await bootstrap_publications(migration, writers_stopped=True)
        assert result == {"status": "applied", "resources": 3, "agents": 1}
        pid = await publisher.scalar(text("select pg_backend_pid()"))

        async def publish():
            row = FileResource(
                channel_id=cid, guid=str(uuid.uuid4()), title_raw="Synthetic after migration", torrent_url="synthetic"
            )
            publisher.add(row)
            await publisher.flush()
            event = await publish_resource(publisher, row.id, kind="created")
            await publisher.commit()
            return row.id, event.sequence

        task = asyncio.create_task(publish())
        try:
            async with asyncio.timeout(10):
                while True:
                    await observer.rollback()
                    wait = await observer.scalar(
                        text("select wait_event_type from pg_stat_activity where pid=:pid"), {"pid": pid}
                    )
                    if wait == "Lock":
                        break
                    assert not task.done(), "publisher escaped migration write barrier"
                    await asyncio.sleep(0.02)
            assert await observer.scalar(select(ResourcePublication.id)) is None
            assert await observer.get(AppSetting, MARKER) is None
            await migration.commit()
            new_id, sequence = await asyncio.wait_for(task, 10)
            assert sequence == 4
            await observer.rollback()
            snapshot = await snapshot_publications(observer, aid, cid)
            assert snapshot.resource_ids == (eligible, new_id)
            await observer.rollback()
            assert await bootstrap_publications(observer, writers_stopped=True) == {"status": "already_applied"}
            await observer.commit()
            assert await snapshot_publications(observer, aid, cid) == snapshot
            print(
                json.dumps(
                    {
                        "migration": result,
                        "rollback_atomic": True,
                        "publisher_lock_observed": True,
                        "published_sequence": sequence,
                        "eligible_after_migration": len(snapshot.resource_ids),
                        "repeat_preserves_progress": True,
                        "data": "synthetic",
                    },
                    indent=2,
                )
            )
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    await engine.dispose()


asyncio.run(main())
