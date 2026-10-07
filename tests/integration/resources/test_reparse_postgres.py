"""Actual PostgreSQL overlapping API creation and bounded reparse sweeps."""

import asyncio
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select, text

from app.api.v1.resources import router
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.resource_reparse_request import ResourceReparseRequest
from app.services import resource_reparse_requests as service


async def test_postgres_overlapping_reparse_creates_one_request(reparse_database, monkeypatch, record_property):
    engine, factory = reparse_database
    async with factory() as db:
        channel = Channel(name="Synthetic reparse", type="rss_feed", url="https://example.invalid", field_mapping={})
        db.add(channel)
        await db.flush()
        resource = FileResource(
            channel_id=channel.id, guid="synthetic", title_raw="Synthetic reparse",
            torrent_url="magnet:?xt=urn:btih:synthetic",
        )
        db.add(resource)
        await db.commit()
        rid = resource.id
    enqueue = AsyncMock(return_value={"job_id": "synthetic"})
    monkeypatch.setattr("app.services.task_queue.task_queue.enqueue", enqueue)
    reached, release = asyncio.Event(), asyncio.Event()
    original = service.create_request

    async def hold_uncommitted_insert(db, resource_id, channel_id):
        request = await original(db, resource_id, channel_id)
        if request:
            reached.set()
            await asyncio.wait_for(release.wait(), 10)
        return request

    monkeypatch.setattr(service, "create_request", hold_uncommitted_insert)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
        first = asyncio.create_task(client.post(f"/api/v1/resources/{rid}/reparse-metadata"))
        second = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            second = asyncio.create_task(client.post(f"/api/v1/resources/{rid}/reparse-metadata"))
            async with engine.connect() as observer:
                async with asyncio.timeout(10):
                    while not await observer.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                        "AND cardinality(pg_blocking_pids(pid)) > 0)"
                    )):
                        await asyncio.sleep(0.02)
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(first, second), 10)
            assert [r.status_code for r in responses] == [200, 409]
            enqueue.assert_awaited_once()
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(ResourceReparseRequest)) == 1
            record_property("actual_postgres_blocking_observed", True)
            record_property("responses", "200,409")
        finally:
            release.set()
            for task in (first, second):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*[t for t in (first, second) if t], return_exceptions=True)
