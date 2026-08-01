"""Async SQLAlchemy database setup."""

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncGenerator, AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Turso (embedded, SQLite-compatible) backend
#
# ``sqlite+aioturso://`` URLs use the pyturso SQLAlchemy dialect with two
# compatibility patches (see ``app/db_turso_dialect.py``).
#
# MVCC concurrent writes are enabled per database file via
# ``PRAGMA journal_mode='mvcc'`` (persistent), and per connection via the
# ``isolation_level=CONCURRENT`` URL query parameter, which makes the driver
# issue ``BEGIN CONCURRENT`` for implicit transactions. Conflicts surface as
# "Write-write conflict" errors and are retried like SQLite lock errors.
# ---------------------------------------------------------------------------

from app.db_turso_dialect import register as _register_turso_dialect  # noqa: E402


def is_turso_url(url: str) -> bool:
    """Whether the given database URL uses the embedded Turso engine."""
    return "turso" in url


def normalize_database_url(url: str) -> str:
    """Append Turso-specific defaults to the URL when missing."""
    if is_turso_url(url) and "isolation_level=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}isolation_level=CONCURRENT"
    return url


_register_turso_dialect()

# ---------------------------------------------------------------------------
# Lock/conflict retry handling
#
# Turso MVCC raises "Write-write conflict" when concurrent transactions touch
# the same rows (and "database is locked" in single-writer paths). Both are
# transient and safe to retry.
#
# We mitigate this with **retry-with-backoff at request/transaction
# boundary** — a FastAPI middleware and a context manager that automatically
# retry on lock/conflict errors, so API handlers and background jobs don't
# need to know about the embedded engine.
# ---------------------------------------------------------------------------

_MAX_DB_RETRIES = 5
_DB_RETRY_BASE_S = 0.125  # 125 ms initial backoff


def _is_retryable_lock_error(exc: Exception) -> bool:
    """Check if an exception is a retryable lock or MVCC write conflict.

    Turso MVCC raises DatabaseError "Write-write conflict" when concurrent
    transactions touch the same rows; single-writer paths can still raise
    "database is locked". Both are transient and safe to retry.
    """
    if not isinstance(exc, DatabaseError):
        return False
    msg = str(exc).lower()
    return "database is locked" in msg or "write-write conflict" in msg


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
        except DatabaseError as e:
            if not _is_retryable_lock_error(e):
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
    On Turso, retries the entire block on lock / write-conflict errors.
    On PostgreSQL, behaves like a plain session (no retry).

    Usage::

        async with committed_session() as session:
            obj = Model(...)
            session.add(obj)
            await session.flush()
            # commit automatically happens on exit; rollback on exception
    """
    if not is_turso_url(settings.database_url):
        # Fast path: no retry needed for PostgreSQL/etc.
        async with async_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return

    # Turso: retry on lock/conflict errors
    last_exc: Exception | None = None
    for attempt in range(_MAX_DB_RETRIES):
        async with async_session_factory() as session:
            try:
                yield session
                await session.commit()
                return
            except DatabaseError as e:
                await session.rollback()
                last_exc = e
                if not _is_retryable_lock_error(e):
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
    """Install a FastAPI middleware that retries requests on lock/conflict errors.

    On PostgreSQL, this is a no-op. On Turso, the middleware catches lock /
    write-conflict DatabaseErrors and retries the entire request with a fresh
    session (5 attempts with exponential backoff).
    """
    if not is_turso_url(settings.database_url):
        return app

    from fastapi import Request, Response

    @app.middleware("http")
    async def _db_lock_retry_middleware(request: Request, call_next):
        last_exc: Exception | None = None
        for attempt in range(_MAX_DB_RETRIES):
            try:
                response: Response = await call_next(request)
                return response
            except DatabaseError as e:
                last_exc = e
                if not _is_retryable_lock_error(e):
                    raise
                if attempt == _MAX_DB_RETRIES - 1:
                    raise
                delay = _backoff_delay(attempt)
                logger.debug("database is locked in request — retrying in %.0f ms (attempt %d/%d)",
                             delay * 1000, attempt + 1, _MAX_DB_RETRIES)
                await asyncio.sleep(delay)
        raise last_exc or AssertionError("unreachable")

    return app


def apply_db_pragmas(async_engine) -> None:
    """Per-connection pragmas. Only Turso needs one (foreign key enforcement);
    MVCC mode is a persistent property of the database file, set by
    ``create_tables`` or the migration script. PostgreSQL needs nothing."""
    url_str = str(async_engine.url)
    if not is_turso_url(url_str):
        return

    @event.listens_for(async_engine.sync_engine, "connect")
    def _set_turso_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()


engine = create_async_engine(
    normalize_database_url(settings.database_url),
    echo=settings.debug,
)
apply_db_pragmas(engine)

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
        if is_turso_url(settings.database_url):
            # MVCC mode is persistent per file and unlocks BEGIN CONCURRENT
            # (isolation_level=CONCURRENT in the URL).
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("PRAGMA journal_mode='mvcc'"))
            # FTS shadow tables + native FTS indexes live on a sidecar
            # database (FTS indexes are incompatible with MVCC mode).
            from app.services.fts import ensure_fts_tables
            await ensure_fts_tables()
            await _apply_light_migrations(conn)
        elif "postgresql" in settings.database_url:
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

    if is_turso_url(settings.database_url):
        # One-time backfill for databases whose FTS shadow tables predate the
        # index introduction (e.g. migrated from SQLite).
        from app.services.fts import backfill_fts_if_empty

        async with async_session_factory() as session:
            await backfill_fts_if_empty(session)
            await session.commit()


async def _apply_light_migrations(conn) -> None:
    """Idempotent ``ADD COLUMN`` migrations for schema evolutions that we don't
    manage via a proper migration tool yet.

    ``Base.metadata.create_all`` only creates missing *tables*; it never ALTERs
    existing ones. This helper adds columns that have appeared on model classes
    since the local database was first created. Each entry is safe to run
    repeatedly: we probe the current columns and skip when the target is
    already there.
    """
    is_turso = is_turso_url(settings.database_url)
    is_postgres = "postgresql" in settings.database_url

    # Column additions: (table, column_name, ddl_type_and_default)
    additions: list[tuple[str, str, str]] = [
        ("file_resources", "is_batch",
         "BOOLEAN NOT NULL DEFAULT 0" if is_turso else "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("file_resources", "episode_start", "INTEGER"),
        ("file_resources", "episode_end", "INTEGER"),
        # subtitle_langs: JSON array of BCP-47 language tags. SQLite stores JSON
        # as TEXT; PostgreSQL has a proper JSONB type.
        ("file_resources", "subtitle_langs", "TEXT" if is_turso else "JSONB"),
        # Episode reconciliation (P2): stores the original absolute-numbering
        # value when the agent converts "S04 - 84" → per-season 13; and a
        # confidence tag noting where the final episode value came from.
        ("file_resources", "absolute_episode", "INTEGER"),
        ("file_resources", "episode_confidence", "VARCHAR(16)"),
        # Agent consumption watermark (P4): latest FileResource.created_at the
        # agent has considered. Delta runs scan only newer resources.
        ("agents", "last_consumed_at", "DATETIME"),
        # Scan-window lower bound recorded on AgentRun for manual windowed
        # runs (NULL = delta/targeted; 1970-01-01 = explicit "no limit").
        ("agent_runs", "scan_since", "DATETIME"),
        # Optional user-supplied LLM candidate-picker instruction.
        ("agents", "llm_prompt", "TEXT"),
        # The candidate the LLM picked for a PendingDecision (resource id).
        ("pending_decisions", "llm_picked_resource_id", "VARCHAR(36)"),
        # Per-channel external metadata source (tmdb/exa/wikipedia/jina/local).
        # NULL → fall back to the default source at runtime.
        ("channels", "metadata_source", "VARCHAR(32)"),
        # Metadata retry state on FileResource: ``metadata_matched_at`` only
        # records successes, so failed attempts looked like "never tried".
        # These let the fetch-time backfill re-run transient failures (with
        # backoff) and long-stale "no match" rows, while skipping correctly
        # unmatched non-work content.
        ("file_resources", "metadata_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("file_resources", "last_metadata_attempt_at", "DATETIME"),
        ("file_resources", "metadata_failure_type", "VARCHAR(16)"),
        # AudioWork link for non-TV/non-movie works (ASMR / music / drama CD /
        # radio). The audio_works table itself is created by create_all.
        ("file_resources", "audio_work_id", "VARCHAR(36)"),
        # Per-channel auto-cleanup of stale unresolved resources: an enable
        # toggle + an age threshold (days, default 21 = 3 weeks).
        ("channels", "auto_cleanup_unresolved_enabled",
         "BOOLEAN NOT NULL DEFAULT 0" if is_turso else "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("channels", "auto_cleanup_unresolved_days", "INTEGER NOT NULL DEFAULT 21"),
        # Logic-generation tag on cached metadata verdicts. Legacy rows get 0
        # (< METADATA_CACHE_GENERATION) and are treated as misses on read.
        ("metadata_cache", "generation", "INTEGER NOT NULL DEFAULT 0"),
    ]

    for table, column, ddl in additions:
        if is_turso:
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
    # Older PostgreSQL DBs may have a native enum restricting
    # ``downloader_instances.type`` to just ``'transmission'``. We now allow
    # ``'mock'`` as well (and the column has been widened to a plain String
    # in the ORM). Turso databases use a plain VARCHAR + CHECK from the start.
    try:
        if is_postgres:
            # Idempotent: succeeds silently if the value is already there.
            await conn.execute(text(
                "ALTER TYPE downloader_type ADD VALUE IF NOT EXISTS 'mock'"
            ))
    except Exception as e:
        logger.warning("[migrate] downloader_type widening skipped: %s", e)

    # ── download_tasks.agent_id → nullable + ON DELETE SET NULL ────────────
    # Older PostgreSQL DBs created the column as ``NOT NULL`` with
    # ``ON DELETE CASCADE``. We now want to keep tasks after an Agent is
    # deleted (marked cancelled) so ``agent_id`` must be nullable. Turso
    # databases are always created from — or migrated after — the new shape.
    try:
        if is_postgres:
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

    # ── one-time non_work reset for the AudioWork path ───────────────────
    # Resources previously classified ``non_work`` (ASMR / music / OP-ED)
    # were never retried. Now that the metadata agent can resolve them into
    # AudioWork entities, clear that marker once so the backfill reprocesses
    # them under the new path. Genuinely-non-work content will simply be
    # reclassified (non_work again or linked to an AudioWork stub). Gated by
    # an app_settings sentinel so it runs exactly once.
    try:
        if is_turso:
            await conn.execute(text(
                "INSERT OR IGNORE INTO app_settings(key, value) "
                "VALUES ('audio_work_non_work_reset', 'pending')"
            ))
        elif is_postgres:
            await conn.execute(text(
                "INSERT INTO app_settings(key, value) "
                "VALUES ('audio_work_non_work_reset', 'pending') "
                "ON CONFLICT (key) DO NOTHING"
            ))
        row = (await conn.execute(text(
            "SELECT value FROM app_settings WHERE key = 'audio_work_non_work_reset'"
        ))).first()
        if row and row[0] == "pending":
            res = await conn.execute(text(
                "UPDATE file_resources SET metadata_failure_type = NULL, "
                "metadata_attempts = 0, last_metadata_attempt_at = NULL "
                "WHERE metadata_failure_type = 'non_work'"
            ))
            await conn.execute(text(
                "UPDATE app_settings SET value = 'done' "
                "WHERE key = 'audio_work_non_work_reset'"
            ))
            logger.info(
                "[migrate] reset %s non_work rows for AudioWork reprocessing",
                getattr(res, "rowcount", "?"),
            )
    except Exception as e:
        logger.warning("[migrate] non_work reset skipped: %s", e)

    # ── one-time not_found reset for improved query cleaning ─────────────
    # The Wikipedia candidate-query cleaner was strengthened (drops paren
    # alt-titles, colon description tails, roman-numeral season markers) and
    # non-media titles are now classified non_work. Reset existing not_found
    # rows once so the backfill reprocesses them under the new logic instead
    # of waiting out the 7-day cooldown.
    try:
        sentinel = "not_found_reclean_reset"
        if is_turso:
            await conn.execute(text(
                f"INSERT OR IGNORE INTO app_settings(key, value) "
                f"VALUES ('{sentinel}', 'pending')"
            ))
        elif is_postgres:
            await conn.execute(text(
                f"INSERT INTO app_settings(key, value) "
                f"VALUES ('{sentinel}', 'pending') ON CONFLICT (key) DO NOTHING"
            ))
        row = (await conn.execute(text(
            f"SELECT value FROM app_settings WHERE key = '{sentinel}'"
        ))).first()
        if row and row[0] == "pending":
            res = await conn.execute(text(
                "UPDATE file_resources SET metadata_failure_type = NULL, "
                "metadata_attempts = 0, last_metadata_attempt_at = NULL "
                "WHERE metadata_failure_type = 'not_found'"
            ))
            await conn.execute(text(
                f"UPDATE app_settings SET value = 'done' WHERE key = '{sentinel}'"
            ))
            logger.info(
                "[migrate] reset %s not_found rows for query re-cleaning",
                getattr(res, "rowcount", "?"),
            )
    except Exception as e:
        logger.warning("[migrate] not_found reset skipped: %s", e)

    # ── one-time not_found reset for auto-link improvements ──────────────
    # The Wikipedia auto-link now matches candidate titles against all
    # queries (fixing page-id dedup) and splits CJK work names from trailing
    # romaji. Reset not_found once more so existing rows are reprocessed
    # under the improved matching.
    try:
        sentinel = "not_found_autolink_reset"
        if is_turso:
            await conn.execute(text(
                f"INSERT OR IGNORE INTO app_settings(key, value) "
                f"VALUES ('{sentinel}', 'pending')"
            ))
        elif is_postgres:
            await conn.execute(text(
                f"INSERT INTO app_settings(key, value) "
                f"VALUES ('{sentinel}', 'pending') ON CONFLICT (key) DO NOTHING"
            ))
        row = (await conn.execute(text(
            f"SELECT value FROM app_settings WHERE key = '{sentinel}'"
        ))).first()
        if row and row[0] == "pending":
            res = await conn.execute(text(
                "UPDATE file_resources SET metadata_failure_type = NULL, "
                "metadata_attempts = 0, last_metadata_attempt_at = NULL "
                "WHERE metadata_failure_type = 'not_found'"
            ))
            await conn.execute(text(
                f"UPDATE app_settings SET value = 'done' WHERE key = '{sentinel}'"
            ))
            logger.info(
                "[migrate] reset %s not_found rows for auto-link reprocessing",
                getattr(res, "rowcount", "?"),
            )
    except Exception as e:
        logger.warning("[migrate] not_found autolink reset skipped: %s", e)
