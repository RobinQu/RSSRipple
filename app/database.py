"""Async SQLAlchemy database setup."""

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncGenerator, AsyncIterator

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
# 1. **Retry-with-backoff at request/transaction boundary** — a FastAPI
#    middleware and a context manager that automatically retry on lock errors,
#    so API handlers and background jobs don't need to know about SQLite.
#
# 2. **busy_timeout + WAL + NORMAL sync** — better concurrency defaults.
# ---------------------------------------------------------------------------

_MAX_DB_RETRIES = 5
_DB_RETRY_BASE_S = 0.125  # 125 ms initial backoff


def _is_sqlite_lock_error(exc: Exception) -> bool:
    """Check if an exception is a SQLite "database is locked" error."""
    if not isinstance(exc, OperationalError):
        return False
    return "database is locked" in str(exc)


def _backoff_delay(attempt: int) -> float:
    """Calculate exponential backoff delay for the given attempt (0-indexed)."""
    return _DB_RETRY_BASE_S * (2 ** attempt) * (1 + random.random() * 0.5)


async def retry_on_lock(coro_factory) -> object:
    """Execute an awaitable *coro_factory*, retrying on "database is locked".

    Usage::

        result = await retry_on_lock(lambda: some_db_operation())

    The *coro_factory* is called fresh on each retry so that a new session
    / connection is used.  (A stale session that already holds a lock
    conflict would fail forever on retry.)

    Prefer using ``committed_session()`` or the auto-retry middleware
    instead of this function directly — it's kept for backward compatibility.
    """
    for attempt in range(_MAX_DB_RETRIES):
        try:
            return await coro_factory()
        except OperationalError as e:
            if not _is_sqlite_lock_error(e):
                raise
            if attempt == _MAX_DB_RETRIES - 1:
                raise
            delay = _backoff_delay(attempt)
            logger.debug("database is locked — retrying in %.0f ms (attempt %d/%d)",
                         delay * 1000, attempt + 1, _MAX_DB_RETRIES)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


@contextlib.asynccontextmanager
async def committed_session() -> AsyncIterator[AsyncSession]:
    """Async context manager for a transactional session with automatic retry.

    Yields an async session, commits on normal exit, rolls back on exception.
    On SQLite, retries the entire block on "database is locked" errors.
    On PostgreSQL, behaves like a plain session (no retry).

    Usage::

        async with committed_session() as session:
            obj = Model(...)
            session.add(obj)
            await session.flush()
            # commit automatically happens on exit; rollback on exception
    """
    if "sqlite" not in settings.database_url:
        # Fast path: no retry needed for PostgreSQL/etc.
        async with async_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return

    # SQLite: retry on lock errors
    last_exc: Exception | None = None
    for attempt in range(_MAX_DB_RETRIES):
        async with async_session_factory() as session:
            try:
                yield session
                await session.commit()
                return
            except OperationalError as e:
                await session.rollback()
                last_exc = e
                if not _is_sqlite_lock_error(e):
                    raise
                if attempt == _MAX_DB_RETRIES - 1:
                    raise
                delay = _backoff_delay(attempt)
                logger.debug("database is locked in committed_session — retrying in %.0f ms (attempt %d/%d)",
                             delay * 1000, attempt + 1, _MAX_DB_RETRIES)
                await asyncio.sleep(delay)
            except Exception:
                await session.rollback()
                raise
    raise last_exc or AssertionError("unreachable")


def install_db_retry_middleware(app):
    """Install a FastAPI middleware that retries requests on SQLite lock errors.

    On PostgreSQL, this is a no-op. On SQLite, the middleware catches
    "database is locked" OperationalErrors and retries the entire request
    with a fresh session (5 attempts with exponential backoff).
    """
    if "sqlite" not in settings.database_url:
        return app

    from fastapi import Request, Response

    @app.middleware("http")
    async def _db_lock_retry_middleware(request: Request, call_next):
        last_exc: Exception | None = None
        for attempt in range(_MAX_DB_RETRIES):
            try:
                response: Response = await call_next(request)
                return response
            except OperationalError as e:
                last_exc = e
                if not _is_sqlite_lock_error(e):
                    raise
                if attempt == _MAX_DB_RETRIES - 1:
                    raise
                delay = _backoff_delay(attempt)
                logger.debug("database is locked in request — retrying in %.0f ms (attempt %d/%d)",
                             delay * 1000, attempt + 1, _MAX_DB_RETRIES)
                await asyncio.sleep(delay)
        raise last_exc or AssertionError("unreachable")

    return app


def _engine_connect_args() -> dict:
    """Extra connection kwargs for SQLite. Ignored for other drivers."""
    if "sqlite" in settings.database_url:
        return {"timeout": 15}  # wait up to 15 s before raising "database is locked"
    return {}


def enable_sqlite_fk(async_engine) -> None:
    """Enable foreign key enforcement and good concurrency defaults for SQLite."""
    if "sqlite" not in str(async_engine.url):
        return

    @event.listens_for(async_engine.sync_engine, "connect")
    def _set_fk_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 15000")  # wait up to 15s before raising "database is locked"
        cursor.execute("PRAGMA journal_mode = WAL")    # concurrent reads with writers
        cursor.execute("PRAGMA synchronous = NORMAL")  # safe under WAL, much faster writes
        cursor.execute("PRAGMA wal_autocheckpoint = 1000")  # keep WAL size manageable
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
            # Create FTS5 virtual tables for CJK-aware full-text search
            from app.services.fts import ensure_fts_tables
            await ensure_fts_tables(conn)
            await _apply_light_migrations(conn)
            return

        if "postgresql" in settings.database_url:
            # Multiple distributed app replicas can start at the same time.
            # PostgreSQL enum DDL is not race-free under concurrent create_all().
            await conn.execute(text("SELECT pg_advisory_lock(72057594037927937)"))
            try:
                await conn.run_sync(Base.metadata.create_all)
                await _apply_light_migrations(conn)
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(72057594037927937)"))
            return

        await conn.run_sync(Base.metadata.create_all)
        await _apply_light_migrations(conn)


async def _apply_light_migrations(conn) -> None:
    """Idempotent ``ADD COLUMN`` migrations for schema evolutions that we don't
    manage via a proper migration tool yet.

    ``Base.metadata.create_all`` only creates missing *tables*; it never ALTERs
    existing ones. This helper adds columns that have appeared on model classes
    since the local database was first created. Each entry is safe to run
    repeatedly: we probe the current columns and skip when the target is
    already there.
    """
    is_sqlite = "sqlite" in settings.database_url
    is_postgres = "postgresql" in settings.database_url

    # Column additions: (table, column_name, ddl_type_and_default)
    additions: list[tuple[str, str, str]] = [
        ("file_resources", "is_batch",
         "BOOLEAN NOT NULL DEFAULT 0" if is_sqlite else "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("file_resources", "episode_start", "INTEGER"),
        ("file_resources", "episode_end", "INTEGER"),
        # subtitle_langs: JSON array of BCP-47 language tags. SQLite stores JSON
        # as TEXT; PostgreSQL has a proper JSONB type.
        ("file_resources", "subtitle_langs", "TEXT" if is_sqlite else "JSONB"),
        # Episode reconciliation (P2): stores the original absolute-numbering
        # value when the agent converts "S04 - 84" → per-season 13; and a
        # confidence tag noting where the final episode value came from.
        ("file_resources", "absolute_episode", "INTEGER"),
        ("file_resources", "episode_confidence", "VARCHAR(16)"),
        # Agent consumption watermark (P4): latest FileResource.created_at the
        # agent has considered. Delta runs scan only newer resources.
        ("agents", "last_consumed_at", "DATETIME"),
        # Optional user-supplied LLM candidate-picker instruction.
        ("agents", "llm_prompt", "TEXT"),
        # The candidate the LLM picked for a PendingDecision (resource id).
        ("pending_decisions", "llm_picked_resource_id", "VARCHAR(36)"),
    ]

    for table, column, ddl in additions:
        if is_sqlite:
            info = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
            existing = {row[1] for row in info}
        elif is_postgres:
            info = (await conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"
            ), {"t": table})).fetchall()
            existing = {row[0] for row in info}
        else:
            # Best-effort for other dialects: just try the ADD and swallow errors.
            existing = set()
        if column in existing:
            continue
        try:
            await conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
            logger.info("[migrate] added column %s.%s", table, column)
        except Exception as e:
            # Non-fatal — race with another replica or dialect quirk.
            logger.warning("[migrate] failed to add %s.%s: %s", table, column, e)

    # ── downloader_type enum widening ────────────────────────────────────
    # Older DBs may have a CHECK constraint restricting
    # ``downloader_instances.type`` to just ``'transmission'``. We now allow
    # ``'mock'`` as well (and the column has been widened to a plain String
    # in the ORM). Rewrite the CHECK / native enum in place.
    try:
        if is_sqlite:
            row = (await conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='downloader_instances'"
            ))).first()
            if row and row[0] and "'transmission'" in row[0] and "CHECK" in row[0].upper():
                # Rewrite the CHECK to a no-op via writable_schema. Safer than a
                # full table rebuild for this specific narrow change.
                new_sql = row[0].replace(
                    "CHECK (type IN ('transmission'))",
                    "CHECK (type IN ('transmission', 'mock'))",
                )
                if new_sql != row[0]:
                    await conn.execute(text("PRAGMA writable_schema = 1"))
                    await conn.execute(text(
                        "UPDATE sqlite_master SET sql = :sql "
                        "WHERE type = 'table' AND name = 'downloader_instances'"
                    ), {"sql": new_sql})
                    await conn.execute(text("PRAGMA writable_schema = 0"))
                    logger.info("[migrate] widened downloader_instances.type CHECK to accept 'mock'")
        elif is_postgres:
            # Idempotent: succeeds silently if the value is already there.
            await conn.execute(text(
                "ALTER TYPE downloader_type ADD VALUE IF NOT EXISTS 'mock'"
            ))
    except Exception as e:
        logger.warning("[migrate] downloader_type widening skipped: %s", e)

    # ── download_tasks.agent_id → nullable + ON DELETE SET NULL ────────────
    # Older DBs created the column as ``NOT NULL`` with ``ON DELETE CASCADE``.
    # We now want to keep tasks after an Agent is deleted (marked cancelled)
    # so ``agent_id`` must be nullable. SQLite can't ALTER column nullability
    # in-place, so rebuild the table when we detect the old shape.
    try:
        if is_sqlite:
            info = (await conn.execute(text("PRAGMA table_info(download_tasks)"))).fetchall()
            agent_col = next((row for row in info if row[1] == "agent_id"), None)
            # row: (cid, name, type, notnull, dflt, pk)
            if agent_col is not None and agent_col[3] == 1:
                logger.info("[migrate] rebuilding download_tasks to make agent_id nullable")
                await conn.execute(text("PRAGMA foreign_keys = OFF"))
                await conn.execute(text("ALTER TABLE download_tasks RENAME TO _download_tasks_old"))
                # Recreate with the new schema (Base.metadata knows the new shape).
                await conn.run_sync(Base.metadata.tables["download_tasks"].create)
                # Copy rows over (column order matches: id, agent_id, ...).
                await conn.execute(text(
                    "INSERT INTO download_tasks SELECT * FROM _download_tasks_old"
                ))
                await conn.execute(text("DROP TABLE _download_tasks_old"))
                await conn.execute(text("PRAGMA foreign_keys = ON"))
        elif is_postgres:
            await conn.execute(text(
                "ALTER TABLE download_tasks ALTER COLUMN agent_id DROP NOT NULL"
            ))
            # Best-effort: drop the old CASCADE FK if it exists, then re-add
            # SET NULL. Names come from create_all so may differ across
            # environments — swallow errors.
            try:
                await conn.execute(text(
                    "ALTER TABLE download_tasks DROP CONSTRAINT IF EXISTS download_tasks_agent_id_fkey"
                ))
                await conn.execute(text(
                    "ALTER TABLE download_tasks "
                    "ADD CONSTRAINT download_tasks_agent_id_fkey "
                    "FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE SET NULL"
                ))
            except Exception:
                pass
    except Exception as e:
        logger.warning("[migrate] download_tasks.agent_id widening skipped: %s", e)

    # ── agents.last_consumed_at backfill ─────────────────────────────────
    # Existing agents get their watermark set to the channel's current max
    # FileResource.created_at so the first delta run after upgrade does NOT
    # silently auto-dispatch every historical matching resource (backfill must
    # be a deliberate, user-selected action via the rules-preview flow). Only
    # touches rows where the column is still NULL.
    try:
        await conn.execute(text(
            "UPDATE agents SET last_consumed_at = COALESCE("
            "  (SELECT MAX(fr.created_at) FROM file_resources fr "
            "   WHERE fr.channel_id = agents.channel_id),"
            "  CURRENT_TIMESTAMP"
            ") WHERE last_consumed_at IS NULL"
        ))
    except Exception as e:
        logger.warning("[migrate] agents.last_consumed_at backfill skipped: %s", e)
