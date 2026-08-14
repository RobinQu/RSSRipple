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
# We mitigate this with **retry-with-backoff at an operation boundary that
# can replay the whole operation** — a FastAPI middleware (re-invokes the
# request), ``retry_on_lock`` (re-invokes a coroutine factory), or callers
# that retry an idempotent block (e.g. organize's auto-execute). A context
# manager *cannot* retry its caller's ``async with`` body (a generator-based
# CM may not yield again after the body throws), so ``committed_session``
# deliberately does not pretend to: it rolls back and re-raises the original
# error.
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

    This is the retry primitive for non-HTTP boundaries (background jobs
    replaying an idempotent operation); HTTP requests are covered by the
    auto-retry middleware installed by ``install_db_retry_middleware``.
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
    """Async context manager for a transactional session.

    Yields an async session, commits on normal exit, rolls back on exception
    and re-raises the ORIGINAL error. There is intentionally no lock/conflict
    retry loop here: a generator-based context manager cannot re-run the
    caller's ``async with`` body after it threw (a second ``yield`` after
    ``athrow()`` raises ``RuntimeError: generator didn't stop after
    athrow()``), so the previous retry loop only ever masked the real
    ``DatabaseError`` behind that RuntimeError. Retry instead at a boundary
    that can replay the whole operation: the HTTP middleware,
    ``retry_on_lock``, or an idempotent caller-level retry.

    Usage::

        async with committed_session() as session:
            obj = Model(...)
            session.add(obj)
            await session.flush()
            # commit automatically happens on exit; rollback on exception
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


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
                await _ensure_pg_trgm_indexes(conn)
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

    # Backfill search_text for rows created before the column/event hook
    # existed (both backends; PostgreSQL needs it for the pg_trgm indexes).
    from app.services.fts import backfill_search_text

    async with async_session_factory() as session:
        await backfill_search_text(session)
        await session.commit()


async def _ensure_pg_trgm_indexes(conn) -> None:
    """pg_trgm GIN indexes over the normalized ``search_text`` columns.

    ``CREATE EXTENSION``/``CREATE INDEX`` are both ``IF NOT EXISTS``-guarded
    and each step is wrapped in ``_best_effort``: a role without the
    ``pg_trgm`` extension privilege must not kill startup — search then falls
    back to the plain ``LIKE``/Python scan.
    """
    async with _best_effort(conn, "pg_trgm extension"):
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    for table in ("tv_series", "movies", "audio_works"):
        async with _best_effort(conn, f"pg_trgm index {table}"):
            await conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_{table}_search_text_trgm "
                f"ON {table} USING gin (search_text gin_trgm_ops)"
            ))


@contextlib.asynccontextmanager
async def _best_effort(conn, label: str) -> AsyncIterator[None]:
    """Run a tolerated-failure migration step inside a SAVEPOINT.

    On PostgreSQL a failed statement aborts the surrounding transaction:
    without a savepoint, one skipped step would make every later step (and
    the final ``pg_advisory_unlock`` in ``create_tables``) fail with
    ``InFailedSQLTransactionError`` and kill application startup.
    """
    try:
        async with conn.begin_nested():
            yield
    except Exception as e:
        logger.warning("[migrate] %s skipped: %s", label, e)


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
        # Per-channel external metadata source (wikipedia/tmdb since P1;
        # legacy exa/jina/local values are converged by the UPDATE below).
        # NULL → fall back to the default source at runtime.
        ("channels", "metadata_source", "VARCHAR(32)"),
        # Ordered Exa-fallback site whitelist for the channel (JSON list of
        # registry source names). NULL → default order; [] → fallback disabled.
        ("channels", "metadata_fallback_sources", "TEXT" if is_turso else "JSONB"),
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
        # Per-channel "default mark as anime" flag — immutable after creation.
        ("channels", "default_is_anime",
         "BOOLEAN NOT NULL DEFAULT 0" if is_turso else "BOOLEAN NOT NULL DEFAULT FALSE"),
        # Logic-generation tag on cached metadata verdicts. Legacy rows get 0
        # (< METADATA_CACHE_GENERATION) and are treated as misses on read.
        ("metadata_cache", "generation", "INTEGER NOT NULL DEFAULT 0"),
        # Per-season episode counts on TVSeries ([{season_number,
        # episode_count}, ...]) so episode reconciliation works on the
        # agent-free link paths (known-work short-circuit, fuzzy auto-link).
        ("tv_series", "seasons", "TEXT" if is_turso else "JSONB"),
        # Release year parsed from the raw title — drives the Layer-3
        # local-match year guard (same-title remakes like 攻壳机动队 2026).
        ("file_resources", "title_year", "INTEGER"),
        # Season on PendingDecision — part of the idempotency key so S1E3
        # and S4E3 of the same series no longer collide.
        ("pending_decisions", "season", "INTEGER"),
        # Franchise grouping (WorkCollection) — the work_collections table
        # itself is created by create_all.
        ("tv_series", "collection_id", "VARCHAR(36)"),
        ("movies", "collection_id", "VARCHAR(36)"),
        # Normalized search haystack (title_cn + title_en + original_title +
        # aliases through normalize_title), maintained by the ORM before_flush
        # hook. Indexed with pg_trgm GIN on PostgreSQL; Turso mirrors it into
        # the FTS sidecar via the fts_outbox drain.
        ("tv_series", "search_text", "TEXT"),
        ("movies", "search_text", "TEXT"),
        ("audio_works", "search_text", "TEXT"),
        # Tri-state anime flag on works (see anime_signals.py). Nullable on
        # purpose: NULL = not yet determined, distinct from False.
        ("tv_series", "is_anime", "BOOLEAN"),
        ("movies", "is_anime", "BOOLEAN"),
        # Daemon-view → process-view path prefix mapping used by the built-in
        # organize subsystem. DEPRECATED orphan column (R1): superseded by the
        # volume binding below; kept in place, no longer read by code.
        ("downloader_instances", "path_map", "TEXT" if is_turso else "JSONB"),
        # Downloader volume binding (R1): daemon-view download_dir root ==
        # volume.mount_path + volume_subpath. Both NULL = identical views
        # (identity). The storage_volumes table itself is created by
        # create_all.
        ("downloader_instances", "volume_id", "VARCHAR(36)"),
        ("downloader_instances", "volume_subpath", "VARCHAR(1024)"),
        # Media-server-derived Library (R2): the library root is now a
        # structured volume reference (volume_id + root_subpath) resolved at
        # use time; root_path/plex_section stay as inert orphan columns. The
        # media_server_instances / media_server_bindings tables themselves
        # are created by create_all.
        ("libraries", "media_server_id", "VARCHAR(36)"),
        ("libraries", "section_key", "VARCHAR(64)"),
        ("libraries", "server_path", "VARCHAR(1024)"),
        ("libraries", "volume_id", "VARCHAR(36)"),
        ("libraries", "root_subpath", "VARCHAR(1024)"),
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
        async with _best_effort(conn, f"add column {table}.{column}"):
            await conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
            logger.info("[migrate] added column %s.%s", table, column)

    # ── agents.notify_webhook_* → agent_webhooks rows ────────────────────
    # Webhook registration moved from three columns on ``agents`` to the
    # ``agent_webhooks`` fan-out table. Copy each legacy registration over
    # once (agents that already have any agent_webhooks row are skipped);
    # the old columns stay in place as inert orphans. Guarded by a column
    # probe so it is a no-op on fresh databases where the legacy columns
    # never existed.
    async with _best_effort(conn, "agents.notify_webhook → agent_webhooks"):
        if is_turso:
            info = (await conn.execute(text("PRAGMA table_info(agents)"))).fetchall()
            agent_cols = {row[1] for row in info}
        elif is_postgres:
            info = (await conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'agents'"
            ))).fetchall()
            agent_cols = {row[0] for row in info}
        else:
            agent_cols = set()
        if {"notify_webhook_url", "notify_webhook_mock"} <= agent_cols:
            import uuid as _uuid

            migrated = {
                row[0]
                for row in (await conn.execute(
                    text("SELECT agent_id FROM agent_webhooks")
                )).fetchall()
            }
            legacy = (await conn.execute(text(
                "SELECT id, notify_webhook_url, notify_webhook_mock FROM agents "
                "WHERE notify_webhook_url IS NOT NULL"
            ))).fetchall()
            copied = 0
            for agent_id, url, mock in legacy:
                if agent_id in migrated:
                    continue
                await conn.execute(text(
                    "INSERT INTO agent_webhooks"
                    "(id, agent_id, url, mock, enabled, created_at, updated_at) "
                    "VALUES (:id, :aid, :url, :mock, :enabled, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"id": str(_uuid.uuid4()), "aid": agent_id, "url": url,
                    "mock": bool(mock), "enabled": True})
                copied += 1
            if copied:
                logger.info(
                    "[migrate] copied %d legacy webhook registrations to agent_webhooks",
                    copied,
                )

    # ── channels.metadata_source two-source convergence ──────────────────
    # Channel metadata sources are now restricted to wikipedia/tmdb (Phase
    # P1). Values set before the convergence (exa/jina/local/combined) would
    # no longer pass API validation; rewrite them to the new default once.
    # Idempotent: only touches non-conforming values. NULL stays NULL (it
    # resolves to the default at runtime).
    async with _best_effort(conn, "channels.metadata_source convergence"):
        await conn.execute(text(
            "UPDATE channels SET metadata_source = 'wikipedia' "
            "WHERE metadata_source IS NOT NULL "
            "AND metadata_source NOT IN ('wikipedia', 'tmdb')"
        ))

    # ── downloader_type enum widening ────────────────────────────────────
    # Older PostgreSQL DBs may have a native enum restricting
    # ``downloader_instances.type`` to just ``'transmission'``. We now allow
    # ``'mock'`` as well (and the column has been widened to a plain String
    # in the ORM). Turso databases use a plain VARCHAR + CHECK from the start.
    async with _best_effort(conn, "downloader_type widening"):
        if is_postgres:
            # Idempotent: succeeds silently if the value is already there.
            await conn.execute(text(
                "ALTER TYPE downloader_type ADD VALUE IF NOT EXISTS 'mock'"
            ))

    # ── download_tasks.agent_id → nullable + ON DELETE SET NULL ────────────
    # Older PostgreSQL DBs created the column as ``NOT NULL`` with
    # ``ON DELETE CASCADE``. We now want to keep tasks after an Agent is
    # deleted (marked cancelled) so ``agent_id`` must be nullable. Turso
    # databases are always created from — or migrated after — the new shape.
    async with _best_effort(conn, "download_tasks.agent_id widening"):
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

    # ── agents.last_consumed_at backfill ─────────────────────────────────
    # Existing agents get their watermark set to the channel's current max
    # FileResource.created_at so the first delta run after upgrade does NOT
    # silently auto-dispatch every historical matching resource (backfill must
    # be a deliberate, user-selected action via the rules-preview flow). Only
    # touches rows where the column is still NULL.
    async with _best_effort(conn, "agents.last_consumed_at backfill"):
        await conn.execute(text(
            "UPDATE agents SET last_consumed_at = COALESCE("
            "  (SELECT MAX(fr.created_at) FROM file_resources fr "
            "   WHERE fr.channel_id = agents.channel_id),"
            "  CURRENT_TIMESTAMP"
            ") WHERE last_consumed_at IS NULL"
        ))

    # ── one-time non_work reset for the AudioWork path ───────────────────
    # Resources previously classified ``non_work`` (ASMR / music / OP-ED)
    # were never retried. Now that the metadata agent can resolve them into
    # AudioWork entities, clear that marker once so the backfill reprocesses
    # them under the new path. Genuinely-non-work content will simply be
    # reclassified (non_work again or linked to an AudioWork stub). Gated by
    # an app_settings sentinel so it runs exactly once.
    async with _best_effort(conn, "non_work reset"):
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

    # ── one-time not_found reset for improved query cleaning ─────────────
    # The Wikipedia candidate-query cleaner was strengthened (drops paren
    # alt-titles, colon description tails, roman-numeral season markers) and
    # non-media titles are now classified non_work. Reset existing not_found
    # rows once so the backfill reprocesses them under the new logic instead
    # of waiting out the 7-day cooldown.
    async with _best_effort(conn, "not_found reset"):
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

    # ── one-time not_found reset for auto-link improvements ──────────────
    # The Wikipedia auto-link now matches candidate titles against all
    # queries (fixing page-id dedup) and splits CJK work names from trailing
    # romaji. Reset not_found once more so existing rows are reprocessed
    # under the improved matching.
    async with _best_effort(conn, "not_found autolink reset"):
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

    # ── work_external_ids identity-bag seed (Phase P3) ───────────────────
    # The identity bag reverse-maps any known (source, external_id) to a work
    # (see app/models/work_external_id.py). Seed it from existing rows: every
    # TVSeries/Movie whose primary external_id/external_source references a
    # registry source gets a bag row (raw, as stored on the column). Idempotent
    # via a Python-side set difference (dialect-agnostic); the table itself is
    # created by create_all.
    async with _best_effort(conn, "work_external_ids seed"):
        import uuid as _uuid

        from app.services.metadata_source_registry import REGISTRY_SOURCES

        existing = {
            (row[0], row[1])
            for row in (await conn.execute(
                text("SELECT source, external_id FROM work_external_ids")
            )).fetchall()
        }
        claimed: set[tuple[str, str]] = set()
        seeds: list[tuple[str, str, str, str]] = []
        for work_type, table in (("series", "tv_series"), ("movie", "movies")):
            for row in (await conn.execute(text(
                f"SELECT id, external_source, external_id FROM {table} "
                "WHERE external_id IS NOT NULL AND external_source IS NOT NULL"
            ))).fetchall():
                work_id, source, ext = row[0], (row[1] or "").strip().lower(), row[2]
                if not ext or source not in REGISTRY_SOURCES:
                    continue
                if (source, ext) in existing or (source, ext) in claimed:
                    # Same id claimed by two existing rows (pre-bag duplicate):
                    # first row wins; the pair stays a dedup candidate.
                    continue
                claimed.add((source, ext))
                seeds.append((work_type, work_id, source, ext))
        for work_type, work_id, source, ext in seeds:
            await conn.execute(text(
                "INSERT INTO work_external_ids(id, work_type, work_id, source, external_id) "
                "VALUES (:id, :wt, :wid, :src, :ext)"
            ), {"id": str(_uuid.uuid4()), "wt": work_type, "wid": work_id,
                "src": source, "ext": ext})
        if seeds:
            logger.info("[migrate] seeded %d work_external_ids rows", len(seeds))

    # ── download_notifications legacy delivery columns ─────────────────
    # The pre-fan-out schema carried ``status``, ``error_message``,
    # ``attempt_count``, ``next_attempt_at``, ``notified_at`` and
    # ``processed_at`` on the ORM. The fan-out refactor removed them, but on
    # existing databases the physical columns remain — and ``status`` /
    # ``attempt_count`` are NOT NULL without defaults, so every insert through
    # the new ORM fails. Turso/SQLite cannot drop NOT NULL in place, so the
    # table is rebuilt with exactly the current model columns; PostgreSQL
    # just drops the NOT NULL constraints and keeps the orphan columns.
    async with _best_effort(conn, "download_notifications legacy columns"):
        if is_turso:
            info = (await conn.execute(
                text("PRAGMA table_info(download_notifications)")
            )).fetchall()
            cols = {row[1] for row in info}
        elif is_postgres:
            info = (await conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'download_notifications'"
            ))).fetchall()
            cols = {row[0] for row in info}
        else:
            cols = set()

        if "status" in cols and is_turso:
            await conn.execute(text(
                "CREATE TABLE download_notifications_new ("
                "id VARCHAR(36) NOT NULL, "
                "agent_id VARCHAR(36), "
                "download_task_id VARCHAR(36) NOT NULL, "
                "payload JSON NOT NULL, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
                "PRIMARY KEY (id), "
                "FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE SET NULL, "
                "UNIQUE (download_task_id), "
                "FOREIGN KEY(download_task_id) REFERENCES download_tasks (id) ON DELETE CASCADE"
                ")"
            ))
            await conn.execute(text(
                "INSERT INTO download_notifications_new "
                "(id, agent_id, download_task_id, payload, created_at, updated_at) "
                "SELECT id, agent_id, download_task_id, payload, created_at, updated_at "
                "FROM download_notifications"
            ))
            # webhook_deliveries references this table but was just created
            # (empty) by create_all, so the implicit DELETE on DROP is a no-op.
            await conn.execute(text("DROP TABLE download_notifications"))
            await conn.execute(text(
                "ALTER TABLE download_notifications_new "
                "RENAME TO download_notifications"
            ))
            logger.info(
                "[migrate] rebuilt download_notifications without legacy delivery columns"
            )
        elif is_postgres:
            for legacy_col in ("status", "attempt_count"):
                if legacy_col in cols:
                    await conn.execute(text(
                        f"ALTER TABLE download_notifications "
                        f"ALTER COLUMN {legacy_col} DROP NOT NULL"
                    ))
                    logger.info(
                        "[migrate] download_notifications.%s NOT NULL dropped", legacy_col
                    )

    # ── libraries.root_path NOT NULL 放宽（R2）───────────────────────────
    # Library 库根改为卷引用（volume_id + root_subpath）动态解析，静态
    # ``root_path`` 列废弃为惰性孤儿。存量库该列是 NOT NULL 且无默认值，
    # 新代码插入扫描派生行不再写它会违例：Turso/SQLite 走表重建（对齐上方
    # download_notifications 先例），PostgreSQL 仅 DROP NOT NULL。
    async with _best_effort(conn, "libraries.root_path nullable"):
        if is_turso:
            info = (await conn.execute(
                text("PRAGMA table_info(libraries)")
            )).fetchall()
            notnull = {row[1]: row[3] for row in info}
            if notnull.get("root_path"):
                await conn.execute(text(
                    "CREATE TABLE libraries_new ("
                    "id VARCHAR(36) NOT NULL, "
                    "name VARCHAR(255) NOT NULL, "
                    "root_path VARCHAR(1024), "
                    "kind VARCHAR(16) NOT NULL, "
                    "plex_section VARCHAR(64), "
                    "subtitle_lang_map JSON, "
                    "media_server_id VARCHAR(36), "
                    "section_key VARCHAR(64), "
                    "server_path VARCHAR(1024), "
                    "volume_id VARCHAR(36), "
                    "root_subpath VARCHAR(1024), "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, "
                    "PRIMARY KEY (id), "
                    "UNIQUE (media_server_id, section_key, server_path), "
                    "FOREIGN KEY(media_server_id) REFERENCES "
                    "media_server_instances (id) ON DELETE SET NULL, "
                    "FOREIGN KEY(volume_id) REFERENCES "
                    "storage_volumes (id) ON DELETE SET NULL"
                    ")"
                ))
                await conn.execute(text(
                    "INSERT INTO libraries_new "
                    "(id, name, root_path, kind, plex_section, subtitle_lang_map, "
                    " media_server_id, section_key, server_path, volume_id, "
                    " root_subpath, created_at, updated_at) "
                    "SELECT id, name, root_path, kind, plex_section, "
                    "subtitle_lang_map, media_server_id, section_key, server_path, "
                    "volume_id, root_subpath, created_at, updated_at "
                    "FROM libraries"
                ))
                # organize_plans / organize_rules reference this table; both
                # use ON DELETE SET NULL and survive the rebuild untouched.
                await conn.execute(text("DROP TABLE libraries"))
                await conn.execute(text(
                    "ALTER TABLE libraries_new RENAME TO libraries"
                ))
                logger.info(
                    "[migrate] rebuilt libraries with nullable root_path "
                    "and media-server columns"
                )
        elif is_postgres:
            await conn.execute(text(
                "ALTER TABLE libraries ALTER COLUMN root_path DROP NOT NULL"
            ))

    # ── libraries.plex_section → section_key ────────────────────────────
    # 刷新寻址列更名（支持多服务器/多类型）；旧列保留为惰性孤儿。幂等：
    # 只拷 section_key 仍为 NULL 的行。
    async with _best_effort(conn, "libraries.plex_section → section_key"):
        await conn.execute(text(
            "UPDATE libraries SET section_key = plex_section "
            "WHERE section_key IS NULL AND plex_section IS NOT NULL"
        ))

    # ── 全局 PLEX_URL/PLEX_TOKEN → MediaServerInstance（R2）──────────────
    # 媒体服务器配置全部入库，全局环境变量移除（对齐 agents.notify_webhook_*
    # → agent_webhooks 的迁移先例）。settings 已删 plex_* 字段，这里直读
    # 环境变量；仅当环境变量存在且实例表为空时插一条 Plex 实例，幂等。
    async with _best_effort(conn, "PLEX_URL/PLEX_TOKEN → media_server_instances"):
        import os as _os
        import uuid as _uuid

        plex_url = _os.environ.get("PLEX_URL")
        plex_token = _os.environ.get("PLEX_TOKEN")
        if plex_url and plex_token:
            count = (await conn.execute(
                text("SELECT COUNT(*) FROM media_server_instances")
            )).scalar_one()
            if count == 0:
                await conn.execute(text(
                    "INSERT INTO media_server_instances"
                    "(id, name, type, url, token, enabled, created_at, updated_at) "
                    "VALUES (:id, :name, 'plex', :url, :token, :enabled, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"id": str(_uuid.uuid4()), "name": "Plex", "url": plex_url,
                    "token": plex_token, "enabled": True})
                logger.info(
                    "[migrate] converted global PLEX_URL/PLEX_TOKEN into a "
                    "media_server_instances row"
                )
