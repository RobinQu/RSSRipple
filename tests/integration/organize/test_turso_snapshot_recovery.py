"""Real Turso SAVEPOINT conflict: a pooled connection must see later commits.

Synthetic counter rows reproduce notify fan-out vs organize execution without
SQL mocks or sleeps. The failed connection is deliberately read, rolled back,
and reused after a second independent commit, as in the automatic pipeline.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import normalize_database_url


@pytest.mark.asyncio
async def test_savepoint_lock_failure_does_not_reuse_stale_snapshot(tmp_path):
    engine = create_async_engine(normalize_database_url(
        f"sqlite+aioturso:///{tmp_path / 'snapshot.db'}"
    ))
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA journal_mode='mvcc'"))
            await connection.execute(text(
                "CREATE TABLE counter (id INTEGER PRIMARY KEY, value INTEGER)"
            ))
            await connection.execute(text("INSERT INTO counter VALUES (1, 0)"))
        async with engine.connect() as reader, engine.connect() as writer:
            statement = text("SELECT value FROM counter WHERE id = 1")
            assert await reader.scalar(statement) == 0
            writer_connection = writer.sync_connection.connection.dbapi_connection
            reader_driver = reader.sync_connection.connection.dbapi_connection.driver_connection
            nested = await reader.begin_nested()
            await writer.execute(text("UPDATE counter SET value = 1 WHERE id = 1"))
            await writer.commit()
            with pytest.raises(OperationalError, match="database is locked"):
                await reader.execute(text("INSERT INTO counter VALUES (2, 0) RETURNING id"))
            assert reader_driver._closed
            await nested.rollback()
            await reader.rollback()
            assert await reader.scalar(statement) == 1
            await reader.rollback()
            await writer.execute(text("UPDATE counter SET value = 2 WHERE id = 1"))
            await writer.commit()
            # Old driver behavior keeps reading 1 even after repeated rollback.
            assert await reader.scalar(statement) == 2
            assert await reader.scalar(text("SELECT count(*) FROM counter")) == 1
            assert writer.sync_connection.connection.dbapi_connection is writer_connection
    finally:
        await engine.dispose()
