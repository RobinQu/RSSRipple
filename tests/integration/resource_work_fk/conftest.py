"""Scratch PostgreSQL databases on an explicitly isolated test server."""
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from sqlalchemy import CheckConstraint, MetaData
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.database import Base


def _schema(request):
    mode = getattr(request, "param", None)
    if mode not in {"legacy", "legacy_no_audio", "drift_threshold", "drift_parentheses"}:
        return Base.metadata
    legacy = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(legacy)
    table = legacy.tables["file_resources"]
    constraint = next(item for item in table.constraints if item.name == "ck_file_resources_work_fk")
    table.constraints.remove(constraint)
    if mode == "drift_threshold":
        from app.models.file_resource import RESOURCE_WORK_FK_CHECK

        table.append_constraint(CheckConstraint(RESOURCE_WORK_FK_CHECK.replace("<= 1", "<= 2"), name="ck_file_resources_work_fk"))
    elif mode == "drift_parentheses":
        # SQLite coerces a comparison to 0/1. This wrong CHECK has the same
        # non-parenthesis tokens as ours but accepts multiple work FKs.
        table.append_constraint(CheckConstraint(
            "CASE WHEN series_id IS NOT NULL THEN 1 ELSE 0 END + "
            "(CASE WHEN movie_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN audio_work_id IS NOT NULL THEN 1 ELSE 0 END <= 1)",
            name="ck_file_resources_work_fk",
        ))
    elif mode == "legacy_no_audio":
        column = table.c.audio_work_id
        for constraint in list(table.foreign_key_constraints):
            if column.name in constraint.column_keys:
                table.constraints.remove(constraint)
                table.foreign_keys.difference_update(constraint.elements)
        # Only fixture metadata is changed; Base.metadata remains intact.
        table._columns.remove(column)
    return legacy


@pytest.fixture
async def work_fk_postgres(monkeypatch, request):
    url = os.environ.get("WORK_FK_TEST_POSTGRES_URL") or os.environ.get("QUEUE_RECOVERY_POSTGRES_URL")
    if not url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Work FK tests require isolated PostgreSQL")
        pytest.skip("Set WORK_FK_TEST_POSTGRES_URL to isolated PostgreSQL")
    parts = urlsplit(url)
    assert parts.path in {"/work_fk_probe", "/queue_recovery"}
    name = "work_fk_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    engine = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        engine = create_async_engine(urlunsplit(parts._replace(scheme="postgresql+asyncpg", path="/" + name)))
        async with engine.begin() as connection:
            await connection.run_sync(_schema(request).create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr("app.database.async_session_factory", factory)
        yield engine, factory
    finally:
        if engine is not None:
            await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture
async def work_fk_turso(tmp_path, monkeypatch, request):
    from sqlalchemy import text

    from app.config import settings
    from app.database import apply_db_pragmas, normalize_database_url
    from app.services import fts

    url = normalize_database_url(f"sqlite+aioturso:///{tmp_path / 'work_fk.db'}")
    engine = create_async_engine(url)
    apply_db_pragmas(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.database.async_session_factory", factory)
    monkeypatch.setattr(settings, "database_url", url)
    sidecar = create_async_engine(
        f"sqlite+aioturso:///{tmp_path / 'work_fk_fts.db'}?experimental_features=index_method",
        pool_size=1, max_overflow=0,
    )
    monkeypatch.setattr(fts, "_FTS_ENGINE", sidecar)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_schema(request).create_all)
            await connection.execute(text("PRAGMA journal_mode='mvcc'"))
        yield engine, factory
    finally:
        await sidecar.dispose()
        await engine.dispose()
