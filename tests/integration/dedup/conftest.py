"""Scratch PostgreSQL databases on an explicitly isolated test server."""
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.database import Base


@pytest.fixture
async def dedup_postgres(monkeypatch):
    url = os.environ.get("DEDUP_TEST_POSTGRES_URL") or os.environ.get("QUEUE_RECOVERY_POSTGRES_URL")
    if not url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Dedup tests require isolated PostgreSQL")
        pytest.skip("Set DEDUP_TEST_POSTGRES_URL to isolated PostgreSQL")
    parts = urlsplit(url)
    assert parts.path in {"/dedup_probe", "/queue_recovery"}
    name = "dedup_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    engine = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        engine = create_async_engine(urlunsplit(parts._replace(scheme="postgresql+asyncpg", path="/" + name)))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr("app.database.async_session_factory", factory)
        yield engine, factory
    finally:
        if engine is not None:
            await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture
async def dedup_turso(tmp_path, monkeypatch):
    from sqlalchemy import text

    from app.config import settings
    from app.database import apply_db_pragmas, normalize_database_url
    from app.services import fts

    url = normalize_database_url(f"sqlite+aioturso:///{tmp_path / 'dedup.db'}")
    engine = create_async_engine(url)
    apply_db_pragmas(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.database.async_session_factory", factory)
    monkeypatch.setattr(settings, "database_url", url)
    sidecar = create_async_engine(
        f"sqlite+aioturso:///{tmp_path / 'dedup_fts.db'}?experimental_features=index_method",
        pool_size=1, max_overflow=0,
    )
    monkeypatch.setattr(fts, "_FTS_ENGINE", sidecar)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("PRAGMA journal_mode='mvcc'"))
        yield engine, factory
    finally:
        await sidecar.dispose()
        await engine.dispose()
