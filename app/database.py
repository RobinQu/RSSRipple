"""Async SQLAlchemy database setup."""

import asyncio
import logging
from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQLite concurrency handling
#
# aiosqlite / the C-level sqlite3 busy_timeout do not work reliably through
# the async bridge: concurrent write attempts raise "database is locked"
# immediately instead of waiting for the configured timeout.
#
# We mitigate this with:
#
# 1. **Retry-with-backoff for write operations** — a utility function
#    ``retry_on_lock`` that wraps a coroutine and retries up to 5× with
#    exponential backoff when it hits "database is locked".  Used at the
#    endpoint/handler level for short write operations.
#
# 2. **busy_timeout + WAL mode** — kept as a second line of defence.
# ---------------------------------------------------------------------------

_MAX_DB_RETRIES = 5
_DB_RETRY_BASE_S = 0.125  # 125 ms initial backoff


async def retry_on_lock(coro_factory) -> object:
    """Execute an awaitable *coro_factory*, retrying on "database is locked".

    Usage::

        result = await retry_on_lock(lambda: some_db_operation())

    The *coro_factory* is called fresh on each retry so that a new session
    / connection is used.  (A stale session that already holds a lock
    conflict would fail forever on retry.)
    """
    import random
    for attempt in range(_MAX_DB_RETRIES):
        try:
            return await coro_factory()
        except OperationalError as e:
            if "database is locked" not in str(e):
                raise
            if attempt == _MAX_DB_RETRIES - 1:
                raise
            delay = _DB_RETRY_BASE_S * (2 ** attempt) * (1 + random.random() * 0.5)
            logger.debug("database is locked — retrying in %.0f ms (attempt %d/%d)",
                         delay * 1000, attempt + 1, _MAX_DB_RETRIES)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _engine_connect_args() -> dict:
    """Extra connection kwargs for SQLite. Ignored for other drivers."""
    if "sqlite" in settings.database_url:
        return {"timeout": 15}  # wait up to 15 s before raising "database is locked"
    return {}


def enable_sqlite_fk(async_engine) -> None:
    """Enable foreign key enforcement for a SQLite async engine."""
    if "sqlite" not in str(async_engine.url):
        return

    @event.listens_for(async_engine.sync_engine, "connect")
    def _set_fk_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 15000")  # wait up to 15s before raising "database is locked"
        cursor.close()


engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    connect_args=_engine_connect_args(),
)
enable_sqlite_fk(engine)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async session, commits on success."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_tables() -> None:
    """Create all database tables (drop-and-recreate dev strategy)."""
    async with engine.begin() as conn:
        if "sqlite" in settings.database_url:
            # WAL mode allows concurrent reads alongside a single writer and
            # dramatically reduces "database is locked" errors under load.
            await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
