"""Real PostgreSQL row-lock serialization of overlapping API membership edits."""

import asyncio
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.agents import router
from app.database import Base
from app.models.agent import Agent
from app.models.agent_work import AgentWork
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.movie import Movie


@pytest.fixture
async def limit_database(monkeypatch):
    url = os.environ.get("AGENT_LIMIT_TEST_POSTGRES_URL") or os.environ.get("QUEUE_RECOVERY_POSTGRES_URL")
    if not url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Agent work limit gate requires isolated PostgreSQL")
        pytest.skip("Use isolated integration PostgreSQL for this test")
    parts = urlsplit(url)
    assert parts.path in {"/agent_limit_probe", "/queue_recovery"}
    name = "agent_limit_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    engine = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        engine = create_async_engine(urlunsplit(parts._replace(scheme="postgresql+asyncpg", path="/"+name)))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr("app.database.async_session_factory", factory)
        yield engine, factory
    finally:
        if engine is not None:
            await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.parametrize("first_edit", ["add", "replace", "narrow"])
async def test_membership_edit_waits_then_rechecks(limit_database, monkeypatch, first_edit, record_property):
    engine, factory = limit_database
    async with factory() as db:
        channel = Channel(name="Synthetic cap channel", type="rss_feed", url=f"https://example.invalid/feed/{str(uuid.uuid4())}", field_mapping={})
        downloader = DownloaderInstance(name="Synthetic unused downloader", type="transmission", url="http://unused.invalid", download_dir="/synthetic-downloads")
        movies = [Movie(title_cn=f"Synthetic movie {i}") for i in range(11)]
        db.add_all([channel, downloader, *movies])
        await db.flush()
        agent = Agent(name="Synthetic cap", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=first_edit == "narrow")
        db.add(agent)
        await db.flush()
        initial_count = 10 if first_edit == "narrow" else 9
        db.add_all([AgentWork(agent_id=agent.id, movie_id=m.id, content_type="movie") for m in movies[:initial_count]])
        await db.commit()
        agent_id = agent.id
        works = [{"movie_id": m.id, "content_type": "movie"} for m in movies]
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    reached = asyncio.Event()
    release = asyncio.Event()
    held = False
    from app.api.v1 import agents as agents_module

    original_lock = agents_module._lock_agent_rules

    async def pause_first_locked_writer(db, identity):
        nonlocal held
        await original_lock(db, identity)
        if not held:
            held = True
            reached.set()
            await asyncio.wait_for(release.wait(), 10)

    # Pause only after the real parent lock is held; the observer below must
    # see the competing request actually blocked by PostgreSQL before release.
    monkeypatch.setattr(agents_module, "_lock_agent_rules", pause_first_locked_writer)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
        first = asyncio.create_task(
            client.post(f"/api/v1/agents/{agent_id}/works", json=works[9]) if first_edit == "add" else
            client.put(f"/api/v1/agents/{agent_id}", json={"works": works[:10]} if first_edit == "replace" else {"scope_channel_wide": False})
        )
        second = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            second = asyncio.create_task(client.post(f"/api/v1/agents/{agent_id}/works", json=works[10]))
            async with engine.connect() as observer:
                async with asyncio.timeout(10):
                    while not await observer.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND cardinality(pg_blocking_pids(pid)) > 0)")):
                        await asyncio.sleep(0.02)
            record_property("real_pg_lock_wait", True)
            release.set()
            responses = await asyncio.gather(first, second)
            assert [r.status_code for r in responses] == [201 if first_edit == "add" else 200, 400]
            async with factory() as db:
                assert await db.scalar(select(func.count()).select_from(AgentWork).where(AgentWork.agent_id == agent_id)) == 10
                assert not (await db.get(Agent, agent_id)).scope_channel_wide
        finally:
            release.set()
            for task in (first, second):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*[t for t in (first, second) if t], return_exceptions=True)
