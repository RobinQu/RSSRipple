"""Explicit isolated PostgreSQL fixtures; no API-suite global sleep patches."""

import asyncio
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import dashboard, resources
from app.database import Base
from app.models.channel import Channel


@pytest.fixture(autouse=True)
def real_reparse_timers(monkeypatch):
    # Other suites import unit/API conftests that replace asyncio.sleep at
    # collection time. Real leases, Redis heartbeats and cancellation must
    # use the standard implementation, including when this suite runs last.
    monkeypatch.setattr(asyncio, "sleep", asyncio.tasks.sleep)


@pytest.fixture
async def reparse_database(monkeypatch):
    url = os.environ.get("REPARSE_TEST_POSTGRES_URL") or os.environ.get("QUEUE_RECOVERY_POSTGRES_URL")
    if not url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Reparse gate requires isolated PostgreSQL")
        pytest.skip("Use isolated integration PostgreSQL for this test")
    parts = urlsplit(url)
    assert parts.path in {"/reparse_probe", "/queue_recovery"}
    name = "reparse_" + uuid.uuid4().hex
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


@pytest.fixture
async def db_session_factory(reparse_database):
    return reparse_database[1]


@pytest.fixture
async def sample_channel(db_session_factory):
    async with db_session_factory() as db:
        channel = Channel(name="Synthetic reparse integration", type="rss_feed", url="https://example.invalid", field_mapping={})
        db.add(channel)
        await db.commit()
        return channel


@pytest.fixture
async def client(db_session_factory):
    app = FastAPI()
    app.include_router(resources.router, prefix="/api/v1")
    app.include_router(dashboard.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
        yield client


@pytest.fixture
async def reparse_turso_database(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import apply_db_pragmas, normalize_database_url
    from app.services import fts

    url = normalize_database_url(f"sqlite+aioturso:///{tmp_path / 'reparse.db'}")
    engine = create_async_engine(url)
    apply_db_pragmas(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.database.async_session_factory", factory)
    monkeypatch.setattr(settings, "database_url", url)
    sidecar = create_async_engine(
        f"sqlite+aioturso:///{tmp_path / 'reparse_fts.db'}?experimental_features=index_method",
        pool_size=1, max_overflow=0,
    )
    monkeypatch.setattr(fts, "_FTS_ENGINE", sidecar)
    try:
        from sqlalchemy import text

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("PRAGMA journal_mode='mvcc'"))
        yield engine, factory
    finally:
        await sidecar.dispose()
        await engine.dispose()
