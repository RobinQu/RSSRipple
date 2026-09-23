"""Synthetic PostgreSQL commit-order matrix; dedicated test database only."""

import asyncio
import json
import os
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.database import Base
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.resource_publication import ChannelPublicationCounter, ResourcePublication
from app.services.resource_publication import publish_resource


async def main():
    url = os.environ["PROBE_DATABASE_URL"]
    assert url.startswith("postgresql+asyncpg://organize_test:organize_test@127.0.0.1:")
    assert url.endswith("/organize_test")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    results = []
    for existing in (False, True):
        for commit in (False, True):
            async with factory() as seed:
                channels = [
                    Channel(name="Synthetic", url="https://example.invalid/" + str(uuid.uuid4()), field_mapping={})
                    for _ in range(2)
                ]
                seed.add_all(channels)
                await seed.flush()
                rows = [
                    FileResource(
                        channel_id=channels[i // 2].id,
                        guid=str(uuid.uuid4()),
                        title_raw="Synthetic",
                        torrent_url="synthetic",
                    )
                    for i in range(3)
                ]
                seed.add_all(rows)
                if existing:
                    seed.add(ChannelPublicationCounter(channel_id=channels[0].id, sequence=0))
                await seed.commit()
            async with factory() as first, factory() as second, factory() as observer:
                await publish_resource(first, rows[0].id, kind="created")
                pid = await second.scalar(text("select pg_backend_pid()"))

                async def publish_second():
                    event = await publish_resource(second, rows[1].id, kind="created")
                    await second.commit()
                    return event.sequence

                task = asyncio.create_task(publish_second())
                try:
                    async with asyncio.timeout(10):
                        while True:
                            await observer.rollback()
                            wait = await observer.scalar(
                                text("select wait_event_type from pg_stat_activity where pid=:pid"), {"pid": pid}
                            )
                            if wait == "Lock":
                                break
                            assert not task.done(), "second publication escaped the first transaction"
                            await asyncio.sleep(0.02)
                    visible = list(
                        await observer.scalars(
                            select(ResourcePublication).where(ResourcePublication.channel_id == channels[0].id)
                        )
                    )
                    assert visible == []
                    async with factory() as unrelated:
                        event = await asyncio.wait_for(publish_resource(unrelated, rows[2].id, kind="created"), 5)
                        await unrelated.commit()
                        assert event.sequence == 1
                    assert not task.done()
                    if commit:
                        await first.commit()
                    else:
                        await first.rollback()
                    seq = await asyncio.wait_for(task, 5)
                    assert seq == (2 if commit else 1)
                    await observer.rollback()
                    events = list(
                        await observer.scalars(
                            select(ResourcePublication)
                            .where(ResourcePublication.channel_id == channels[0].id)
                            .order_by(ResourcePublication.sequence)
                        )
                    )
                    assert [e.sequence for e in events] == ([1, 2] if commit else [1])
                    assert all(e.origin_sequence == e.sequence for e in events)
                    assert events[-1].resource_id == rows[1].id
                    results.append(
                        {
                            "existing_counter": existing,
                            "first_commits": commit,
                            "second_lock_observed": True,
                            "independent_channel_committed": True,
                            "visible_sequences": [e.sequence for e in events],
                        }
                    )
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
    await engine.dispose()
    print(json.dumps({"cases": results, "data": "synthetic", "scope": "publication foundation only"}, indent=2))


asyncio.run(main())
