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
    session (5 attempts with exponential backoff). Only safe HTTP methods
    (GET/HEAD/OPTIONS) are replayed — replaying POST/PATCH/PUT/DELETE could
    duplicate out-of-band side effects (enqueue, downloader RPC, SSE), so
    those surface the error for the client to retry. See
    ``app/middleware/db_retry.py``.
    """
    if not is_turso_url(settings.database_url):
        return app

    from app.middleware.db_retry import DatabaseRetryMiddleware

    app.add_middleware(DatabaseRetryMiddleware)
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


# Startup DDL must never wait indefinitely: PostgreSQL queues lock requests
# strictly, so one blocked ALTER (e.g. behind a long-running worker
# transaction) makes every later reader of that table queue behind it and
# gridlocks the whole stack. Fail fast and retry instead — between attempts
# no lock request of ours is queued, so normal traffic is never stuck behind
# a waiting migration.
_DDL_LOCK_TIMEOUT_MS = 5000
_DDL_MAX_ATTEMPTS = 36  # ~3 min worst case (5s lock_timeout + backoff) before giving up


def _is_lock_timeout(exc: BaseException) -> bool:
    """SQLSTATE 55P03 (lock_not_available) raised when lock_timeout fires."""
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) == "55P03"


async def _create_tables_postgres() -> None:
    """PostgreSQL branch of ``create_tables`` with bounded lock waits."""
    for attempt in range(1, _DDL_MAX_ATTEMPTS + 1):
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(f"SET LOCAL lock_timeout = '{_DDL_LOCK_TIMEOUT_MS}'")
                )
                # Multiple distributed app replicas can start at the same time.
                # PostgreSQL enum DDL is not race-free under concurrent create_all().
                #
                # Transaction-scoped advisory lock: released automatically at
                # COMMIT/ROLLBACK of this engine.begin() block. A plain
                # pg_advisory_lock + explicit unlock would (a) mask any inner
                # failure behind InFailedSQLTransactionError when the unlock ran
                # on an aborted transaction, and (b) strand a session-level lock
                # on the pooled connection after a rollback.
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(72057594037927937)")
                )
                from app.services.decision_schema import prepare_existing_decision_schema

                await prepare_existing_decision_schema(conn)
                await conn.run_sync(Base.metadata.create_all)
                await _apply_light_migrations(conn)
                from app.services.schema_foreign_keys import repair_postgres_foreign_keys

                await repair_postgres_foreign_keys(conn)
                await _ensure_pg_trgm_indexes(conn)
            return
        except DatabaseError as e:
            if not _is_lock_timeout(e) or attempt == _DDL_MAX_ATTEMPTS:
                raise
            logger.warning(
                "[migrate] startup DDL lock unavailable (attempt %d/%d), retrying",
                attempt,
                _DDL_MAX_ATTEMPTS,
            )
            await asyncio.sleep(2)


async def create_tables() -> None:
    """Create all database tables (drop-and-recreate dev strategy)."""
    # Register the light-migration ledger model (app/models/schema_migration.py)
    # on Base.metadata so create_all below emits ``schema_migrations`` on both
    # fresh and upgraded databases. Not re-exported from app.models.__init__.
    import app.models.schema_migration  # noqa: F401

    if "postgresql" in settings.database_url:
        await _create_tables_postgres()
        from app.services.collection_lifecycle import backfill_orphan_collections

        await backfill_orphan_collections()
        # Backfill search_text for rows created before the column/event hook
        # existed (needed for the pg_trgm indexes).
        from app.services.fts import backfill_search_text

        async with async_session_factory() as session:
            await backfill_search_text(session)
            await session.commit()
        return

    # Existing embedded tables must pass review before any startup backfill.
    # Own a fresh ordinary transaction: schema repair cannot use CONCURRENT.
    from app.services.decision_schema import prepare_existing_decision_schema

    async with engine.begin() as conn:
        await conn.execute(text("BEGIN"))
        await prepare_existing_decision_schema(conn)

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
        else:
            await conn.run_sync(Base.metadata.create_all)
            await _apply_light_migrations(conn)

    # The PostgreSQL branch returned above. Embedded schema DDL must own
    # an ordinary transaction; implicit CONCURRENT writes cannot host DDL.
    from app.services.metadata_cache_schema import upgrade_metadata_cache_keys
    from app.services.resource_work_schema import upgrade_sqlite_resource_work_fk

    async with engine.begin() as conn:
        await conn.execute(text("BEGIN"))
        await upgrade_metadata_cache_keys(conn)

    await upgrade_sqlite_resource_work_fk(engine)
    # Rebuild-based hardening (episodes composite FK, channels NOT NULL) —
    # needs an ordinary transaction and, for parent tables, suspended FK
    # enforcement; cannot live inside the light-migration transaction.
    await upgrade_sqlite_table_invariants(engine)
    if is_turso_url(settings.database_url):
        from app.services.schema_foreign_keys import repair_turso_foreign_keys

        await repair_turso_foreign_keys(engine)

    from app.services.collection_lifecycle import backfill_orphan_collections

    await backfill_orphan_collections()

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


# conn.info key holding the in-flight light-migration ledger: the list of
# block labels that completed successfully during the current
# ``_apply_light_migrations`` run. Flushed to the ``schema_migrations`` table
# once at the end of the function (see ``_flush_migration_ledger``).
_LEDGER_INFO_KEY = "rssripple_light_migration_blocks"


def _mark_light_migration_applied(conn, label: str) -> None:
    """Note that a best-effort block completed, for the ledger flush.

    No-op outside ``_apply_light_migrations`` (e.g. the pg_trgm index
    helper), so the hook is safe on shared code paths.
    """
    info = getattr(conn, "info", None)
    if info is None:
        # Duck-typed test doubles may not emulate the info dict.
        return
    blocks = info.get(_LEDGER_INFO_KEY)
    if blocks is not None:
        blocks.append(label)


@contextlib.asynccontextmanager
async def _best_effort(conn, label: str) -> AsyncIterator[None]:
    """Run a tolerated-failure migration step inside a SAVEPOINT.

    On PostgreSQL a failed statement aborts the surrounding transaction:
    without a savepoint, one skipped step would make every later step fail
    with ``InFailedSQLTransactionError`` and kill application startup.

    The savepoint is started *before* the body runs so that failures during
    savepoint creation surface as plain log lines instead of the cryptic
    ``RuntimeError: generator didn't yield`` (which is how
    ``@asynccontextmanager`` reports an exception raised before ``yield``).
    If the savepoint itself cannot be created the step runs unprotected —
    it will fail loudly if the transaction really is unusable.

    A body that completes without error is recorded in the light-migration
    ledger (``_mark_light_migration_applied``); skipped/failed bodies are
    not, so the ``schema_migrations`` table only ever claims blocks that
    actually ran to completion.
    """
    try:
        savepoint = await conn.begin_nested().start()
    except Exception as e:
        logger.warning("[migrate] %s: savepoint unavailable (%s), running unprotected", label, e)
        savepoint = None
        try:
            yield
        except Exception as e2:
            logger.warning("[migrate] %s skipped: %s", label, e2)
        else:
            _mark_light_migration_applied(conn, label)
        return
    try:
        yield
    except Exception as e:
        logger.warning("[migrate] %s skipped: %s", label, e)
        with contextlib.suppress(Exception):
            await savepoint.rollback()
    else:
        with contextlib.suppress(Exception):
            await savepoint.commit()
        _mark_light_migration_applied(conn, label)


async def _flush_migration_ledger(conn, names: list[str]) -> None:
    """Persist the applied-block ledger into the ``schema_migrations`` table.

    Deferred to the end of ``_apply_light_migrations`` on purpose: on MVCC
    Turso a ``BEGIN CONCURRENT`` transaction rejects DDL once it has seen
    DML, so all ledger INSERTs must come after the last DDL block of the
    migration. The rows commit with the surrounding migration transaction,
    so the ledger never claims blocks whose transaction rolled back.
    Re-runs are no-ops (dialect insert-if-missing), keeping ``applied_at``
    at the first successful application. Failures are contained in a
    savepoint + warning: the ledger is auxiliary observability and must
    never gate startup.
    """
    names = list(dict.fromkeys(names))
    if not names:
        return
    if conn.dialect.name == "sqlite":
        stmt = text(
            "INSERT OR IGNORE INTO schema_migrations (name) VALUES (:name)"
        )
    else:
        stmt = text(
            "INSERT INTO schema_migrations (name) VALUES (:name) "
            "ON CONFLICT (name) DO NOTHING"
        )
    async with _best_effort(conn, "schema_migrations ledger flush"):
        for name in names:
            await conn.execute(stmt, {"name": name})


async def _rebuild_sqlite_table(
    conn, table_name: str, *, column_sql: dict[str, str] | None = None
) -> None:
    """Rebuild a Turso/SQLite table from the current ORM metadata.

    SQLite cannot alter column defaults, FK actions, or add columns with
    non-constant defaults in place; the only route is create-new/copy/rename
    (see the download_notifications / libraries rebuilds below for hardcoded
    predecessors). The CREATE TABLE comes from ``Base.metadata`` so the new
    shape always matches the models. Only model columns are carried over —
    these tables must not have intentional orphan columns. ``column_sql``
    overrides the SELECT expression per column (e.g. backfill a new column
    from an existing one). Indexes declared in metadata are recreated after
    the rename. Callers must ensure no other table references the rebuilt
    one as a parent.
    """
    from sqlalchemy.schema import CreateIndex, CreateTable

    table = Base.metadata.tables[table_name]
    info = (await conn.execute(text(f"PRAGMA table_info({table_name})"))).fetchall()
    existing = {row[1] for row in info}
    model_cols = [c.name for c in table.columns]
    overrides = column_sql or {}
    missing = [c for c in model_cols if c not in existing and c not in overrides]
    if missing:
        raise RuntimeError(
            f"cannot rebuild {table_name}: no source for new column(s) {missing}"
        )
    ddl = str(CreateTable(table).compile(dialect=conn.dialect))
    marker = f"CREATE TABLE {table_name} ("
    if marker not in ddl:
        raise RuntimeError(f"unexpected CREATE TABLE shape for {table_name}")
    temporary = f"__rebuild_{table_name}"
    # Clean up a leftover from a previously interrupted rebuild.
    await conn.execute(text(f"DROP TABLE IF EXISTS {temporary}"))
    await conn.execute(text(ddl.replace(marker, f"CREATE TABLE {temporary} (", 1)))
    await conn.execute(text(
        f"INSERT INTO {temporary} ({', '.join(model_cols)}) "
        f"SELECT {', '.join(overrides.get(c, c) for c in model_cols)} FROM {table_name}"
    ))
    await conn.execute(text(f"DROP TABLE {table_name}"))
    await conn.execute(text(f"ALTER TABLE {temporary} RENAME TO {table_name}"))
    for index in table.indexes:
        await conn.execute(text(str(CreateIndex(index).compile(dialect=conn.dialect))))


async def _sqlite_unique_covers(conn, table: str, columns: tuple[str, ...]) -> bool:
    """Whether the SQLite table already has a unique index/constraint exactly
    covering ``columns`` (in order) — including unnamed constraint
    autoindexes, which ``CREATE INDEX IF NOT EXISTS`` cannot detect by name.
    """
    for row in (await conn.execute(text(f"PRAGMA index_list({table})"))).fetchall():
        if not row[2]:  # unique flag
            continue
        cols = [r[2] for r in (await conn.execute(
            text(f"PRAGMA index_info({row[1]})")
        )).fetchall()]
        if tuple(cols) == columns:
            return True
    return False


# Re-tag episodes whose season disagrees with the parent work's
# season_number, but only when the target (series_id, season, episode) slot
# is free — colliding rows are pre-split legacy multi-season state and must
# be resolved by the season-split migration, not by guessing. Dialect-agnostic;
# idempotent (a conforming row matches neither EXISTS branch).
_EPISODE_SEASON_RETAG_SQL = (
    "UPDATE episodes SET season = ("
    "  SELECT ts.season_number FROM tv_series ts"
    "  WHERE ts.id = episodes.series_id) "
    "WHERE EXISTS ("
    "  SELECT 1 FROM tv_series ts"
    "  WHERE ts.id = episodes.series_id"
    "    AND ts.season_number <> episodes.season) "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM episodes e2"
    "  WHERE e2.series_id = episodes.series_id"
    "    AND e2.episode = episodes.episode"
    "    AND e2.season = ("
    "      SELECT ts.season_number FROM tv_series ts"
    "      WHERE ts.id = episodes.series_id))"
)

_EPISODE_SEASON_VIOLATIONS_SQL = (
    "SELECT e.id FROM episodes e JOIN tv_series ts "
    "ON ts.id = e.series_id AND ts.season_number <> e.season LIMIT 5"
)


# Light-migration column additions: (table, column_name, ddl). This module-
# level tuple is the single authoritative source — ``_apply_light_migrations``
# iterates it directly and nothing may append to it at runtime (a stray
# ``additions.append(...)`` once drifted a column away from the reviewed
# list). ``ddl`` is either a type/default string used on every backend, or a
# ``(sqlite_ddl, postgres_ddl)`` tuple when the dialects differ (SQLite has
# no JSONB and spells boolean defaults ``0``/``FALSE`` differently).
_LIGHT_COLUMN_ADDITIONS: tuple[tuple[str, str, str | tuple[str, str]], ...] = (
    ("organize_configuration", "lock_domain", "VARCHAR(36)"),
    ("organize_plans", "file_op", "VARCHAR(16)"),
    ("organize_plans", "needs_category", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("organize_plans", "manual_destination", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("organize_plans", "revision", "BIGINT NOT NULL DEFAULT 0"),
    ("organize_plans", "config_revision", "BIGINT"),
    ("organize_plans", "owner_token", "VARCHAR(36)"),
    ("file_resources", "is_batch",
     ("BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE")),
    ("file_resources", "episode_start", "INTEGER"),
    ("file_resources", "episode_end", "INTEGER"),
    # subtitle_langs: JSON array of BCP-47 language tags. SQLite stores JSON
    # as TEXT; PostgreSQL has a proper JSONB type.
    ("file_resources", "subtitle_langs", ("TEXT", "JSONB")),
    # Canonical subtitle/release-group members.  The legacy scalar
    # subtitle_group column remains available as a raw compatibility value
    # until the migration script has completed and external clients have
    # moved to the plural field.
    ("file_resources", "subtitle_groups", ("TEXT", "JSONB")),
    ("file_resources", "subtitle_groups_source", "VARCHAR(16)"),
    # Episode reconciliation (P2): stores the original absolute-numbering
    # value when the agent converts "S04 - 84" → per-season 13; and a
    # confidence tag noting where the final episode value came from.
    ("file_resources", "absolute_episode", "INTEGER"),
    ("file_resources", "episode_confidence", "VARCHAR(16)"),
    # Agent consumption watermark (P4): latest FileResource.created_at the
    # agent has considered. Delta runs scan only newer resources.
    ("agents", "last_consumed_at", "DATETIME"),
    ("agents", "current_run_token", "VARCHAR(36)"),
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
    # Ordered web-search fallback site whitelist for the channel (JSON list of
    # registry source names). NULL → default order; [] → fallback disabled.
    ("channels", "metadata_fallback_sources", ("TEXT", "JSONB")),
    # Channel-declared required work-metadata fields (JSON list of catalog
    # keys). NULL → unrestricted (legacy); [] → agent filters locked to
    # resource-level fields only.
    ("channels", "required_metadata_fields", ("TEXT", "JSONB")),
    # Metadata retry state on FileResource: ``metadata_matched_at`` only
    # records successes, so failed attempts looked like "never tried".
    # These let the fetch-time backfill re-run transient failures (with
    # backoff) and long-stale "no match" rows, while skipping correctly
    # unmatched non-work content.
    ("file_resources", "metadata_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("file_resources", "last_metadata_attempt_at", "DATETIME"),
    ("file_resources", "metadata_failure_type", "VARCHAR(16)"),
    ("file_resources", "confirmation_ignored_at", ("DATETIME", "TIMESTAMP")),
    # AudioWork link for non-TV/non-movie works (ASMR / music / drama CD /
    # radio). The audio_works table itself is created by create_all.
    ("file_resources", "audio_work_id", "VARCHAR(36) REFERENCES audio_works(id) ON DELETE SET NULL"),
    # Per-channel auto-cleanup of stale unresolved resources: an enable
    # toggle + an age threshold (days, default 21 = 3 weeks).
    ("channels", "auto_cleanup_unresolved_enabled",
     ("BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE")),
    ("channels", "auto_cleanup_unresolved_days", "INTEGER NOT NULL DEFAULT 21"),
    # Per-channel "default mark as anime" flag — immutable after creation.
    ("channels", "default_is_anime",
     ("BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE")),
    # Logic-generation tag on cached metadata verdicts. Legacy rows get 0
    # (< METADATA_CACHE_GENERATION) and are treated as misses on read.
    ("metadata_cache", "generation", "INTEGER NOT NULL DEFAULT 0"),
    # Per-season episode counts on TVSeries ([{season_number,
    # episode_count}, ...]) so episode reconciliation works on the
    # agent-free link paths (known-work short-circuit, fuzzy auto-link).
    ("tv_series", "seasons", ("TEXT", "JSONB")),
    # Release year parsed from the raw title — drives the Layer-3
    # local-match year guard (same-title remakes like 攻壳机动队 2026).
    ("file_resources", "title_year", "INTEGER"),
    # Season on PendingDecision — part of the idempotency key so S1E3
    # and S4E3 of the same series no longer collide.
    ("pending_decisions", "season", "INTEGER"),
    # Franchise grouping (WorkCollection) — the work_collections table
    # itself is created by create_all.
    ("tv_series", "collection_id", "VARCHAR(36) REFERENCES work_collections(id)"),
    ("movies", "collection_id", "VARCHAR(36) REFERENCES work_collections(id)"),
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
    # Fields the user edited manually through the work detail edit form.
    # JSON list of field names — auto metadata scans (upsert / refresh)
    # skip writing these unless the refresh action opts into overriding.
    ("tv_series", "manually_edited_fields", ("TEXT", "JSONB")),
    ("movies", "manually_edited_fields", ("TEXT", "JSONB")),
    # Daemon-view → process-view path prefix mapping used by the built-in
    # organize subsystem. DEPRECATED orphan column (R1): superseded by the
    # volume binding below; kept in place, no longer read by code.
    ("downloader_instances", "path_map", ("TEXT", "JSONB")),
    # Downloader volume binding (R1): daemon-view download_dir root ==
    # volume.mount_path + volume_subpath. Both NULL = identical views
    # (identity). The storage_volumes table itself is created by
    # create_all.
    ("downloader_instances", "volume_id", "VARCHAR(36) REFERENCES storage_volumes(id) ON DELETE SET NULL"),
    ("downloader_instances", "volume_subpath", "VARCHAR(1024)"),
    # Media-server-derived Library (R2): the library root is now a
    # structured volume reference (volume_id + root_subpath) resolved at
    # use time; root_path/plex_section stay as inert orphan columns. The
    # media_server_instances / media_server_bindings tables themselves
    # are created by create_all.
    ("libraries", "media_server_id", "VARCHAR(36) REFERENCES media_server_instances(id) ON DELETE SET NULL"),
    ("libraries", "section_key", "VARCHAR(64)"),
    ("libraries", "server_path", "VARCHAR(1024)"),
    ("libraries", "volume_id", "VARCHAR(36) REFERENCES storage_volumes(id) ON DELETE SET NULL"),
    ("libraries", "root_subpath", "VARCHAR(1024)"),
    # 回收站目录（卷内相对路径）：合集 move 计划的剩余文件整体移入；
    # NULL = 原地保留。
    ("libraries", "recycle_subpath", "VARCHAR(1024)"),
    # Torrent content detection (P1): batch scope sub-classification on
    # FileResource (NULL = non-batch; season / multi_season / franchise),
    # a WorkCollection link for franchise packs, and the local relative
    # path of the cached .torrent file (bytes live on disk only).
    ("file_resources", "batch_scope", "VARCHAR(16)"),
    ("file_resources", "collection_id", "VARCHAR(36) REFERENCES work_collections(id)"),
    ("file_resources", "torrent_file", "VARCHAR(2048)"),
    # Seasons covered by a multi_season/franchise batch pack (JSON int
    # list, persisted from torrent content analysis) — drives the strict
    # content-coverage dedup of batch resources in the agent runner.
    ("file_resources", "batch_seasons", ("TEXT", "JSONB")),
    # Ordered candidate-preference rules on Agent (JSON FieldCondition
    # list): deterministic ranking layer ahead of the LLM pick in
    # conflict resolution (ranks only, never filters).
    ("agents", "pick_preferences", ("TEXT", "JSONB")),
    # Per-season episode ranges of a batch resource
    # ([{season, episode_start, episode_end}, ...]) from the torrent
    # content analysis / the edit wizard. Recomputed from file
    # assignments whenever those change.
    ("file_resources", "season_ranges", ("TEXT", "JSONB")),
    # Per-channel periodic work-metadata refresh (off by default; legacy
    # rows converge to disabled — the old global auto-refresh toggle is
    # gone). NULL interval → DEFAULT_METADATA_REFRESH_INTERVAL_MINUTES.
    ("channels", "metadata_refresh_enabled",
     ("BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE")),
    ("channels", "metadata_refresh_interval_minutes", "INTEGER"),
    ("channels", "metadata_refresh_full_scope",
     ("BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE")),
    # Per-season works (作品单季化 P2): one TVSeries row = exactly one
    # season of the IP; 0 = specials (Plex Specials convention). The
    # legacy seasons/number_of_seasons columns stay as inert orphans. The
    # partial unique index is ensured below after this column exists.
    ("tv_series", "season_number", "INTEGER NOT NULL DEFAULT 1"),
    # WorkCollection upgraded to the series-level metadata carrier:
    # alias list, normalized search haystack (before_flush hook only —
    # never enqueued into fts_outbox), and the manual-edit guard list.
    ("work_collections", "aliases", ("TEXT", "JSONB")),
    ("work_collections", "search_text", "TEXT"),
    ("work_collections", "manually_edited_fields", ("TEXT", "JSONB")),
    # Magnet metadata resolution state on FileResource: NULL status =
    # never attempted; the worker claims rows via a guarded UPDATE so
    # only one process resolves a given magnet at a time.
    ("file_resources", "magnet_resolve_status", "VARCHAR(16)"),
    ("file_resources", "magnet_resolve_attempt_id", "VARCHAR(36)"),
    ("file_resources", "magnet_resolve_error", "TEXT"),
    ("file_resources", "magnet_resolve_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("file_resources", "magnet_resolve_updated_at", ("DATETIME", "TIMESTAMP")),
    # Custom tracker list for resolution attempts (NULL = defaults only).
    ("file_resources", "magnet_resolve_trackers", ("TEXT", "JSONB")),
    # Optional API-key expiry (NULL = never expires); expired keys are
    # rejected by the auth middleware.
    ("api_keys", "expires_at", ("DATETIME", "TIMESTAMP")),
    # Webhook delivery attempt ownership token — safety-critical (the
    # ADD failure must abort startup, see the mandatory set in the
    # application loop below).
    ("webhook_deliveries", "attempt_token", "VARCHAR(36)"),
)


async def _apply_light_migrations(conn) -> None:
    """Idempotent ``ADD COLUMN`` migrations for schema evolutions that we don't
    manage via a proper migration tool yet.

    ``Base.metadata.create_all`` only creates missing *tables*; it never ALTERs
    existing ones. This helper adds columns that have appeared on model classes
    since the local database was first created. Each entry is safe to run
    repeatedly: we probe the current columns and skip when the target is
    already there.

    Applied-block ledger (``schema_migrations``): every named block that runs
    to completion is recorded once (best-effort blocks via the ``_best_effort``
    hook, mandatory/delegated steps by direct ``applied_blocks.append``; new
    blocks must follow suit, using a stable unique label). The ledger is
    observability/audit only — probes above remain the sole correctness
    authority and are never short-circuited by a ledger row, so damaged or
    restored databases still self-heal. See docs/design/db-migration.md.
    """
    # Inspect the actual connection, including legacy SQLite migrations; a
    # settings override must not select another backend's catalog queries.
    is_turso = conn.dialect.name == "sqlite"
    is_postgres = conn.dialect.name == "postgresql"
    from app.utils.sql_time import utc_now_sql

    now_sql = utc_now_sql(conn.dialect.name)


    # Required consistency guard, not a best-effort metadata backfill.
    from app.services.resource_parent_guard import ensure_resource_parent_guards

    await conn.run_sync(ensure_resource_parent_guards)

    # ── light-migration ledger (schema_migrations) ─────────────────────
    # Each named block below that runs to completion is recorded once in
    # the schema_migrations table (flushed at the end of this function).
    # The ledger is auxiliary observability/audit — NEVER the correctness
    # authority: every block still probes the live schema/data on each
    # startup and self-heals, so a ledger row that disagrees with reality
    # (e.g. a restored backup missing a column the ledger claims) does not
    # suppress re-application. Best-effort blocks are recorded by the
    # ``_best_effort`` hook; mandatory/delegated steps append directly.
    applied_blocks: list[str] = ["resource parent guards"]
    conn.info[_LEDGER_INFO_KEY] = applied_blocks
    # Registered on Base.metadata by create_tables before create_all; the
    # checkfirst create covers direct callers (tests, scripts) whose
    # schema predates the model import. DDL placement is safe: nothing
    # above has issued DML in this transaction.
    import app.models.schema_migration  # noqa: F401

    async with _best_effort(conn, "schema_migrations ledger table"):
        await conn.run_sync(
            lambda sync_conn: Base.metadata.tables["schema_migrations"].create(
                sync_conn, checkfirst=True
            )
        )

    if is_postgres:
        # Media files commonly exceed PostgreSQL INTEGER's 2 GiB ceiling.
        # Turso INTEGER is already a signed 64-bit value.
        async with _best_effort(conn, "organize plan op size bigint"):
            await conn.execute(text(
                "ALTER TABLE organize_plan_ops ALTER COLUMN size TYPE BIGINT"
            ))

    # Column additions: the authoritative list is the module-level
    # ``_LIGHT_COLUMN_ADDITIONS`` tuple (single source — nothing may append
    # to it at runtime). Each ``ddl`` is either a plain type/default string
    # or a ``(sqlite_ddl, postgres_ddl)`` pair.
    for table, column, ddl_spec in _LIGHT_COLUMN_ADDITIONS:
        ddl = (
            ddl_spec[0] if is_turso else ddl_spec[1]
        ) if isinstance(ddl_spec, tuple) else ddl_spec
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
            # The probe remains the correctness authority; the ledger records
            # the confirmed-present column without short-circuiting the probe.
            applied_blocks.append(f"add column {table}.{column}")
            continue
        if table in ("organize_plans", "organize_configuration", "webhook_deliveries") or (
            table == "file_resources" and column == "magnet_resolve_attempt_id"
        ) or (table == "agents" and column == "current_run_token"):
            # Ownership/version columns are safety-critical: a partial schema
            # must fail startup rather than silently run old semantics.
            await conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
            applied_blocks.append(f"add column {table}.{column}")
            logger.info("[migrate] added column %s.%s", table, column)
            continue
        async with _best_effort(conn, f"add column {table}.{column}"):
            # Success is recorded in the ledger by the _best_effort hook.
            await conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
            logger.info("[migrate] added column %s.%s", table, column)

    # ── hot FK secondary indexes (P1-D5) ───────────────────────────────
    # SQLite/Turso never auto-indexes FK columns; each entry was
    # EXPLAIN-verified at synthetic 16k-row scale on both backends (see
    # docs/plans/p0-and-backlog/V28-HOT-FK-INDEXES.md). Names match the
    # Index() definitions on the models so fresh and upgraded databases
    # converge on the same schema, and rebuild-based migrations
    # (``table.indexes``) preserve them.
    #
    # Placement is load-bearing: on MVCC Turso a BEGIN CONCURRENT
    # transaction rejects DDL once it has seen DML (see
    # ``upgrade_sqlite_table_invariants``). Everything above is probe
    # SELECTs or DDL (the column additions included), so these CREATE
    # INDEX statements still run on upgraded databases; the DML-bearing
    # blocks (row backfills, ``ensure_configuration``, table rebuilds)
    # all come later.
    #
    # Each index is gated on a column-existence probe (same style as the
    # column additions above): a legacy table shape can lack the target
    # column when it is added by a later migration/rebuild path, and an
    # unconditional CREATE INDEX on a missing column aborts the upgrade
    # (Turso parses eagerly and rejects the statement). Skipped indexes
    # converge idempotently — the rebuild path recreates them from
    # ``table.indexes`` when the column arrives, and the next startup
    # re-probes and creates any still-missing index.
    hot_fk_indexes = (
        ("ix_file_resources_series_id", "file_resources", ("series_id",),
         "CREATE INDEX IF NOT EXISTS ix_file_resources_series_id "
         "ON file_resources (series_id)"),
        ("ix_file_resources_movie_id", "file_resources", ("movie_id",),
         "CREATE INDEX IF NOT EXISTS ix_file_resources_movie_id "
         "ON file_resources (movie_id)"),
        ("ix_file_resources_audio_work_id", "file_resources", ("audio_work_id",),
         "CREATE INDEX IF NOT EXISTS ix_file_resources_audio_work_id "
         "ON file_resources (audio_work_id)"),
        ("ix_file_resources_collection_id", "file_resources", ("collection_id",),
         "CREATE INDEX IF NOT EXISTS ix_file_resources_collection_id "
         "ON file_resources (collection_id)"),
        ("ix_agent_works_agent_id", "agent_works", ("agent_id",),
         "CREATE INDEX IF NOT EXISTS ix_agent_works_agent_id "
         "ON agent_works (agent_id)"),
        ("ix_agent_works_series_id", "agent_works", ("series_id",),
         "CREATE INDEX IF NOT EXISTS ix_agent_works_series_id "
         "ON agent_works (series_id)"),
        ("ix_agent_works_movie_id", "agent_works", ("movie_id",),
         "CREATE INDEX IF NOT EXISTS ix_agent_works_movie_id "
         "ON agent_works (movie_id)"),
        ("ix_pending_decisions_series_id", "pending_decisions", ("series_id",),
         "CREATE INDEX IF NOT EXISTS ix_pending_decisions_series_id "
         "ON pending_decisions (series_id)"),
        ("ix_pending_decisions_movie_id", "pending_decisions", ("movie_id",),
         "CREATE INDEX IF NOT EXISTS ix_pending_decisions_movie_id "
         "ON pending_decisions (movie_id)"),
        ("ix_download_tasks_file_resource_id", "download_tasks", ("file_resource_id",),
         "CREATE INDEX IF NOT EXISTS ix_download_tasks_file_resource_id "
         "ON download_tasks (file_resource_id)"),
        ("ix_agent_runs_agent_started", "agent_runs", ("agent_id", "started_at"),
         "CREATE INDEX IF NOT EXISTS ix_agent_runs_agent_started "
         "ON agent_runs (agent_id, started_at)"),
        ("ix_webhook_deliveries_status_created", "webhook_deliveries", ("status", "created_at"),
         "CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_status_created "
         "ON webhook_deliveries (status, created_at)"),
    )
    index_column_cache: dict[str, set[str] | None] = {}
    for index_name, table, index_columns, index_ddl in hot_fk_indexes:
        if table not in index_column_cache:
            if is_turso:
                info = (await conn.execute(
                    text(f"PRAGMA table_info({table})")
                )).fetchall()
                index_column_cache[table] = {row[1] for row in info}
            elif is_postgres:
                info = (await conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t"
                ), {"t": table})).fetchall()
                index_column_cache[table] = {row[0] for row in info}
            else:
                # Other dialects: no catalog probe — the CREATE INDEX runs
                # under _best_effort and a missing column skips the block.
                index_column_cache[table] = None
        existing_columns = index_column_cache[table]
        if existing_columns is not None and not set(index_columns) <= existing_columns:
            logger.info(
                "[migrate] skipped index %s: %s lacks column(s) %s "
                "(converges via the rebuild path or the next startup probe)",
                index_name, table, sorted(set(index_columns) - existing_columns),
            )
            continue
        async with _best_effort(conn, f"hot FK index {index_name}"):
            await conn.execute(text(index_ddl))

    # ── tv_series (id, season_number) unique ─────────────────────────
    # Redundant with the PK, but it gives the episodes composite FK
    # ((series_id, season) → (id, season_number)) a unique target so
    # "Episode.season == parent season_number" is DB-enforced. create_all
    # emits it for fresh databases (model UniqueConstraint); existing ones
    # get it here. id is the PK, so the pair is unique by construction —
    # no duplicate probe needed.
    async with _best_effort(conn, "tv_series (id, season_number) unique"):
        if is_postgres:
            present = (await conn.execute(text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conrelid = 'tv_series'::regclass AND contype = 'u' "
                "AND conname = 'uq_tv_series_id_season_number'"
            ))).scalar()
            if not present:
                await conn.execute(text(
                    "ALTER TABLE tv_series ADD CONSTRAINT "
                    "uq_tv_series_id_season_number UNIQUE (id, season_number)"
                ))
                logger.info("[migrate] added tv_series (id, season_number) unique constraint")
        elif is_turso:
            if not await _sqlite_unique_covers(conn, "tv_series", ("id", "season_number")):
                await conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_tv_series_id_season_number "
                    "ON tv_series (id, season_number)"
                ))
                logger.info("[migrate] created unique index uq_tv_series_id_season_number")

    # ── episodes.season server default ─────────────────────────────────
    # The model ships ``server_default="1"``; databases created earlier have a
    # bare ``season INTEGER NOT NULL``. PostgreSQL SET DEFAULT is in-place and
    # cheap; Turso cannot alter a default, so the table is rebuilt (probe the
    # pragma first — fresh databases already carry the default).
    async with _best_effort(conn, "episodes.season default"):
        if is_postgres:
            await conn.execute(text(
                "ALTER TABLE episodes ALTER COLUMN season SET DEFAULT 1"
            ))
        elif is_turso:
            info = (await conn.execute(text("PRAGMA table_info(episodes)"))).fetchall()
            season_row = next((row for row in info if row[1] == "season"), None)
            if season_row is not None and season_row[4] is None:
                # The metadata now carries the composite FK (series_id, season)
                # → tv_series(id, season_number): a rebuild fails mid-copy when
                # legacy rows disagree with the parent work's season. Those
                # databases are fixed and rebuilt by
                # upgrade_sqlite_table_invariants after this transaction.
                violations = (await conn.execute(text(
                    "SELECT e.id FROM episodes e JOIN tv_series ts "
                    "ON ts.id = e.series_id AND ts.season_number <> e.season LIMIT 1"
                ))).fetchall()
                if violations:
                    logger.info(
                        "[migrate] deferred episodes rebuild: rows violate "
                        "season == season_number (handled post-transaction)"
                    )
                else:
                    await _rebuild_sqlite_table(conn, "episodes")
                    logger.info("[migrate] rebuilt episodes with season DEFAULT 1")

    # ── episodes composite FK (PostgreSQL) ───────────────────────────
    # (series_id, season) → tv_series(id, season_number), DEFERRABLE
    # INITIALLY DEFERRED + ON DELETE CASCADE — DB-level enforcement of
    # "Episode.season 恒等于父作品 season_number" (the deferral lets season
    # corrections re-tag parent and children within one transaction). Rows
    # that disagree are first re-tagged to the parent's season_number when
    # the target (series, season, episode) slot is free; unfixable
    # collisions (pre-split legacy multi-season rows) keep the table
    # unconstrained with a warning — finish the season-split migration
    # first. The embedded-SQLite counterpart lives in
    # upgrade_sqlite_table_invariants (it needs an ordinary transaction for
    # the table rebuild). Idempotent: skipped once the constraint exists.
    if is_postgres:
        async with _best_effort(conn, "episodes composite FK"):
            present = (await conn.execute(text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conrelid = 'episodes'::regclass AND contype = 'f' "
                "AND conname = 'fk_episodes_series_season'"
            ))).scalar()
            if not present:
                await conn.execute(text(_EPISODE_SEASON_RETAG_SQL))
                violations = (await conn.execute(
                    text(_EPISODE_SEASON_VIOLATIONS_SQL)
                )).fetchall()
                if violations:
                    logger.warning(
                        "[migrate] skipped episodes composite FK: rows %s violate "
                        "season == season_number; finish the season-split "
                        "migration and restart",
                        [v[0] for v in violations],
                    )
                else:
                    await conn.execute(text(
                        "ALTER TABLE episodes ADD CONSTRAINT fk_episodes_series_season "
                        "FOREIGN KEY (series_id, season) "
                        "REFERENCES tv_series (id, season_number) "
                        "ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED"
                    ))
                    # The composite FK subsumes the legacy plain series_id FK.
                    await conn.execute(text(
                        "ALTER TABLE episodes "
                        "DROP CONSTRAINT IF EXISTS episodes_series_id_fkey"
                    ))
                    logger.info("[migrate] added episodes composite FK (series_id, season)")

    # ── subtitle_group_mappings redundant index ────────────────────────
    # normalized_key's UniqueConstraint already backs lookups; the legacy
    # index=True single-column index is redundant. No-op on fresh databases
    # (the model no longer declares it).
    async with _best_effort(conn, "subtitle_group_mappings redundant index"):
        await conn.execute(text(
            "DROP INDEX IF EXISTS ix_subtitle_group_mappings_normalized_key"
        ))

    # ── external_id VARCHAR(100) → VARCHAR(128) (PostgreSQL only) ──────
    # Unified with WorkExternalId.external_id (128). Turso does not enforce
    # VARCHAR length, so only PostgreSQL needs the ALTER; the probe keeps it
    # a no-op once widened.
    if is_postgres:
        async with _best_effort(conn, "external_id length 128"):
            rows = (await conn.execute(text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_name = ANY(CAST(:tables AS text[])) "
                "AND column_name = 'external_id' "
                "AND character_maximum_length < 128"
            ), {"tables": ["tv_series", "movies", "work_collections", "audio_works"]})).fetchall()
            for (table,) in rows:
                await conn.execute(text(
                    f"ALTER TABLE {table} ALTER COLUMN external_id TYPE VARCHAR(128)"
                ))
                logger.info("[migrate] widened %s.external_id to VARCHAR(128)", table)

    # ── work_external_ids.updated_at ───────────────────────────────────
    # Audit column aligned with the other models (server_default/onupdate
    # current UTC). SQLite rejects ADD COLUMN with a non-constant
    # CURRENT_TIMESTAMP default, so legacy Turso tables are rebuilt with
    # existing rows backfilled from created_at; PostgreSQL adds the column
    # in place (the UTC-default converger below normalizes the expression).
    async with _best_effort(conn, "work_external_ids.updated_at"):
        if is_turso:
            info = (await conn.execute(
                text("PRAGMA table_info(work_external_ids)")
            )).fetchall()
            if "updated_at" not in {row[1] for row in info}:
                await _rebuild_sqlite_table(
                    conn, "work_external_ids",
                    column_sql={"updated_at": "created_at"},
                )
                logger.info("[migrate] rebuilt work_external_ids with updated_at")
        elif is_postgres:
            present = (await conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'work_external_ids' AND column_name = 'updated_at'"
            ))).scalar()
            if not present:
                await conn.execute(text(
                    "ALTER TABLE work_external_ids ADD COLUMN updated_at TIMESTAMP"
                ))
                await conn.execute(text(
                    "UPDATE work_external_ids SET updated_at = created_at"
                ))
                await conn.execute(text(
                    "ALTER TABLE work_external_ids ALTER COLUMN updated_at SET NOT NULL"
                ))
                await conn.execute(text(
                    "ALTER TABLE work_external_ids "
                    f"ALTER COLUMN updated_at SET DEFAULT {now_sql}"
                ))
                logger.info("[migrate] added column work_external_ids.updated_at")

    # ── agent_works target FKs: SET NULL → CASCADE ─────────────────────
    # The XOR check constraint requires exactly one target, so SET NULL on
    # work deletion would violate it and block the delete. Deleting a
    # subscribed work must cascade its subscription rows (the work-delete API
    # already 409-blocks and the dedup merge repoints; this is the DB-level
    # backstop). PostgreSQL swaps the constraint in place; Turso rebuilds.
    async with _best_effort(conn, "agent_works cascade FK"):
        if is_postgres:
            for column, target in (("series_id", "tv_series"), ("movie_id", "movies")):
                fks = (await conn.execute(text(
                    "SELECT conname, confdeltype FROM pg_constraint "
                    "WHERE conrelid = 'agent_works'::regclass AND contype = 'f' "
                    "AND confrelid = CAST(:target AS regclass)"
                ), {"target": target})).fetchall()
                # asyncpg returns PG "char" columns as bytes.
                if any(row[1] in ("c", b"c") for row in fks):
                    continue
                for row in fks:
                    await conn.execute(text(
                        f'ALTER TABLE agent_works DROP CONSTRAINT "{row[0]}"'
                    ))
                await conn.execute(text(
                    f"ALTER TABLE agent_works ADD CONSTRAINT agent_works_{column}_fkey "
                    f"FOREIGN KEY ({column}) REFERENCES {target}(id) ON DELETE CASCADE"
                ))
                logger.info(
                    "[migrate] agent_works.%s FK switched to ON DELETE CASCADE", column
                )
        elif is_turso:
            fks = (await conn.execute(
                text("PRAGMA foreign_key_list(agent_works)")
            )).fetchall()
            if any(
                row[2] in ("tv_series", "movies") and row[6] != "CASCADE"
                for row in fks
            ):
                await _rebuild_sqlite_table(conn, "agent_works")
                logger.info("[migrate] rebuilt agent_works with ON DELETE CASCADE targets")

    # ── uniqueness backfill for legacy databases ───────────────────────
    # create_all emits these UniqueConstraints only for brand-new databases.
    # Existing databases get an equivalent unique index here — but only after
    # a duplicate probe: a conflicting database keeps its rows (warning
    # logged, index skipped) instead of silently losing data. Databases that
    # already enforce the column set (fresh constraint or prior index) are
    # detected via the inspector so no redundant second index is created.
    unique_specs = (
        ("channels", ("url",), "uq_channels_url"),
        ("downloader_instances", ("name",), "uq_downloader_instances_name"),
        ("downloader_instances", ("url",), "uq_downloader_instances_url"),
        ("media_server_instances", ("name",), "uq_media_server_instances_name"),
        ("media_server_instances", ("url",), "uq_media_server_instances_url"),
        ("media_server_bindings", ("server_id", "server_path_prefix"),
         "uq_media_server_bindings_server_prefix"),
    )

    def _covered_uniques(sync_conn):
        from sqlalchemy import inspect as _inspect

        inspector = _inspect(sync_conn)
        covered = set()
        for table, columns, _name in unique_specs:
            if any(
                tuple(uc.get("column_names") or ()) == columns
                for uc in inspector.get_unique_constraints(table)
            ) or any(
                ix.get("unique") and tuple(ix.get("column_names") or ()) == columns
                for ix in inspector.get_indexes(table)
            ):
                covered.add((table, columns))
        return covered

    async with _best_effort(conn, "unique constraint backfill"):
        covered = await conn.run_sync(_covered_uniques)
        for table, columns, name in unique_specs:
            if (table, columns) in covered:
                continue
            col_list = ", ".join(columns)
            dupes = (await conn.execute(text(
                f"SELECT {col_list} FROM {table} "
                f"GROUP BY {col_list} HAVING COUNT(*) > 1 LIMIT 5"
            ))).fetchall()
            if dupes:
                logger.warning(
                    "[migrate] skipped %s on %s(%s): duplicate rows exist "
                    "(first: %s); resolve the duplicates and restart",
                    name, table, col_list, dupes[0],
                )
                continue
            await conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({col_list})"
            ))
            logger.info("[migrate] created unique index %s on %s(%s)", name, table, col_list)

    # ── work_collections identity partial unique index ───────────────
    # The former plain UniqueConstraint on (external_source, external_id)
    # with both columns nullable let every (…, NULL) duplicate through —
    # but shell collections (series_group, NULL id), franchise packs and
    # manual collections are legitimately plural, so uniqueness applies to
    # identity-bearing rows only: partial unique index
    # ``WHERE external_id IS NOT NULL`` (identical DDL on both backends).
    # The legacy PG constraint of the same name is dropped first (its
    # backing index would collide with CREATE INDEX); Turso keeps its inert
    # sqlite_autoindex (weaker, harmless). Duplicate identities keep their
    # rows (warning, index skipped) instead of failing startup.
    async with _best_effort(conn, "work_collections identity unique"):
        dupes = (await conn.execute(text(
            "SELECT external_source, external_id FROM work_collections "
            "WHERE external_id IS NOT NULL "
            "GROUP BY external_source, external_id HAVING COUNT(*) > 1 LIMIT 5"
        ))).fetchall()
        if dupes:
            logger.warning(
                "[migrate] skipped work_collections identity index: duplicate "
                "(external_source, external_id) rows exist (first: %s); resolve "
                "the duplicates and restart",
                dupes[0],
            )
        else:
            if is_postgres:
                await conn.execute(text(
                    "ALTER TABLE work_collections "
                    "DROP CONSTRAINT IF EXISTS uq_work_collections_source_external"
                ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_work_collections_source_external "
                "ON work_collections (external_source, external_id) "
                "WHERE external_id IS NOT NULL"
            ))

    # Required row invariant after legacy work-FK columns have been added.
    from app.services.resource_work_schema import ensure_resource_work_fk_guard

    if is_postgres:
        await ensure_resource_work_fk_guard(conn)
        applied_blocks.append("resource work FK guard")
        from app.services.utc_schema import ensure_utc_timestamp_defaults

        await ensure_utc_timestamp_defaults(conn, Base.metadata)
        applied_blocks.append("UTC timestamp defaults")

        from app.services.metadata_cache_schema import upgrade_metadata_cache_keys

        await upgrade_metadata_cache_keys(conn)

    from app.services.search_text_schema import ensure_search_text_columns

    await ensure_search_text_columns(conn)
    applied_blocks.append("search_text columns")

    # Match fresh-schema uniqueness on upgraded tables. Do not silently
    # continue without this invariant or guess which legacy row to discard:
    # conflicting databases must finish the season-split migration first.
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tv_series_collection_season "
        "ON tv_series (collection_id, season_number) "
        "WHERE collection_id IS NOT NULL"
    ))
    applied_blocks.append("uq_tv_series_collection_season")

    from app.services.organize_config_events import ensure_configuration

    await conn.run_sync(ensure_configuration)
    applied_blocks.append("organize configuration singleton")

    # Dashboard hot-path indexes.  Keep these in the light migration as
    # create_all only creates indexes for brand-new databases.
    for index_ddl in (
        "CREATE INDEX IF NOT EXISTS ix_file_resources_confirmation_created "
        "ON file_resources (confirmation_ignored_at, created_at, id)",
        "CREATE INDEX IF NOT EXISTS ix_pending_decisions_status_created "
        "ON pending_decisions (status, created_at, id)",
        "CREATE INDEX IF NOT EXISTS ix_download_tasks_status_agent "
        "ON download_tasks (status, agent_id)",
        "CREATE INDEX IF NOT EXISTS ix_download_tasks_downloader_torrent "
        "ON download_tasks (downloader_id, transmission_torrent_id)",
    ):
        async with _best_effort(conn, "dashboard query index"):
            await conn.execute(text(index_ddl))

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
                    f"{now_sql}, {now_sql})"
                ), {"id": str(_uuid.uuid4()), "aid": agent_id, "url": url,
                    "mock": bool(mock), "enabled": True})
                copied += 1
            if copied:
                logger.info(
                    "[migrate] copied %d legacy webhook registrations to agent_webhooks",
                    copied,
                )

    # ── channels.metadata_source convergence ───────────────────────────
    # Channel metadata sources are restricted to wikipedia/tmdb/bangumi.
    # Values set before the convergence (exa/jina/local/combined) no longer
    # pass API validation; rewrite them to the default.
    # Idempotent: only touches non-conforming values. NULL stays NULL (it
    # resolves to the default at runtime).
    async with _best_effort(conn, "channels.metadata_source convergence"):
        await conn.execute(text(
            "UPDATE channels SET metadata_source = 'wikipedia' "
            "WHERE metadata_source IS NOT NULL "
            "AND metadata_source NOT IN ('wikipedia', 'tmdb', 'bangumi')"
        ))

    # ── global works-metadata auto-refresh settings removal ────────────
    # The periodic works refresh moved to a per-channel opt-in
    # (channels.metadata_refresh_*). The legacy global toggle/interval and
    # the default-source fallback are gone; drop their stored rows so stale
    # values cannot resurrect the behavior. The Exa MCP fallback endpoint /
    # switch rows are equally dead since the fallback moved to wigolo
    # (wigolo_base_url / wigolo_api_token / web_fallback_enabled).
    async with _best_effort(conn, "global metadata auto-refresh settings cleanup"):
        await conn.execute(text(
            "DELETE FROM app_settings WHERE key IN "
            "('metadata_auto_refresh_enabled', "
            "'metadata_auto_refresh_interval_minutes', "
            "'default_metadata_source', "
            "'exa_mcp_url', 'exa_enabled', 'jina_api_key', 'jina_enabled')"
        ))
        await conn.execute(text(
            "DELETE FROM metadata_cache WHERE source = 'metadata_agent:jina'"
        ))

    # ── channels.required_metadata_fields title_cn compatibility ─────────
    # title_cn is no longer part of the baseline for new channels, but it was
    # previously locked. Preserve it on every channel that existed before this
    # change so the add-only contract does not silently loosen legacy Agents.
    async with _best_effort(conn, "channels.title_cn compatibility"):
        import json as _json

        sentinel = "required_fields_title_cn_compat_v1"
        if is_turso:
            await conn.execute(text(
                "INSERT OR IGNORE INTO app_settings(key, value) "
                "VALUES (:key, 'pending')"
            ), {"key": sentinel})
        elif is_postgres:
            await conn.execute(text(
                "INSERT INTO app_settings(key, value) VALUES (:key, 'pending') "
                "ON CONFLICT (key) DO NOTHING"
            ), {"key": sentinel})
        marker = (await conn.execute(text(
            "SELECT value FROM app_settings WHERE key = :key"
        ), {"key": sentinel})).scalar_one_or_none()
        if marker == "pending":
            rows = await conn.execute(text(
                "SELECT id, required_metadata_fields FROM channels"
            ))
            changed = 0
            for row in rows:
                raw = row.required_metadata_fields
                if isinstance(raw, str):
                    try:
                        current = _json.loads(raw)
                    except ValueError:
                        current = []
                else:
                    current = list(raw or [])
                if "title_cn" in current:
                    continue
                payload = _json.dumps(["title_cn", *current])
                if is_postgres:
                    await conn.execute(text(
                        "UPDATE channels SET required_metadata_fields = "
                        "CAST(:val AS JSONB) WHERE id = :id"
                    ), {"val": payload, "id": row.id})
                else:
                    await conn.execute(text(
                        "UPDATE channels SET required_metadata_fields = :val "
                        "WHERE id = :id"
                    ), {"val": payload, "id": row.id})
                changed += 1
            await conn.execute(text(
                "UPDATE app_settings SET value = 'done' WHERE key = :key"
            ), {"key": sentinel})
            if changed:
                logger.info("[migrate] preserved legacy title_cn requirement on %d channels", changed)

    # ── channels.required_metadata_fields baseline convergence ───────────
    # The required-fields list is mandatory and add-only after creation:
    # every channel must carry at least the code-enforced baseline (base
    # title/type/batch/year/anime fields plus the shape-scoped TV episode
    # fields and the franchise-pack collection link). Legacy NULL rows and
    # partial lists are converged once here: NULL → baseline; existing lists
    # gain any missing locked keys; every row is reordered into canonical
    # catalog order so the stacked display column renders deterministically.
    # Idempotent: converged rows normalize to themselves on re-run.
    # ``title_cn`` and ``title_en`` used to be part of the locked baseline.
    # Remove each once from existing channel declarations so those rows do not
    # keep producing confirmations after the fields become opt-in. Sentinels
    # are required: users may explicitly add either field again under the
    # add-only policy.
    async with _best_effort(conn, "channels.title_cn unlock"):
        import json as _json

        sentinel = "required_fields_title_cn_unlock_v1"
        if is_turso:
            await conn.execute(text(
                "INSERT OR IGNORE INTO app_settings(key, value) "
                "VALUES (:key, 'pending')"
            ), {"key": sentinel})
        elif is_postgres:
            await conn.execute(text(
                "INSERT INTO app_settings(key, value) VALUES (:key, 'pending') "
                "ON CONFLICT (key) DO NOTHING"
            ), {"key": sentinel})
        marker = (await conn.execute(text(
            "SELECT value FROM app_settings WHERE key = :key"
        ), {"key": sentinel})).scalar_one_or_none()
        if marker == "pending":
            rows = await conn.execute(text(
                "SELECT id, required_metadata_fields FROM channels"
            ))
            changed = 0
            for row in rows:
                raw = row.required_metadata_fields
                if isinstance(raw, str):
                    try:
                        current = _json.loads(raw)
                    except ValueError:
                        current = []
                else:
                    current = list(raw or [])
                updated = [key for key in current if key != "title_cn"]
                if updated == current:
                    continue
                payload = _json.dumps(updated)
                if is_postgres:
                    await conn.execute(text(
                        "UPDATE channels SET required_metadata_fields = "
                        "CAST(:val AS JSONB) WHERE id = :id"
                    ), {"val": payload, "id": row.id})
                else:
                    await conn.execute(text(
                        "UPDATE channels SET required_metadata_fields = :val "
                        "WHERE id = :id"
                    ), {"val": payload, "id": row.id})
                changed += 1
            await conn.execute(text(
                "UPDATE app_settings SET value = 'done' WHERE key = :key"
            ), {"key": sentinel})
            if changed:
                logger.info("[migrate] removed legacy title_cn requirement from %d channels", changed)

    async with _best_effort(conn, "channels.title_en unlock"):
        import json as _json

        sentinel = "required_fields_title_en_unlock_v1"
        if is_turso:
            await conn.execute(text(
                "INSERT OR IGNORE INTO app_settings(key, value) "
                "VALUES (:key, 'pending')"
            ), {"key": sentinel})
        elif is_postgres:
            await conn.execute(text(
                "INSERT INTO app_settings(key, value) VALUES (:key, 'pending') "
                "ON CONFLICT (key) DO NOTHING"
            ), {"key": sentinel})
        marker = (await conn.execute(text(
            "SELECT value FROM app_settings WHERE key = :key"
        ), {"key": sentinel})).scalar_one_or_none()
        if marker == "pending":
            rows = await conn.execute(text(
                "SELECT id, required_metadata_fields FROM channels"
            ))
            changed = 0
            for row in rows:
                raw = row.required_metadata_fields
                if isinstance(raw, str):
                    try:
                        current = _json.loads(raw)
                    except ValueError:
                        current = []
                else:
                    current = list(raw or [])
                updated = [key for key in current if key != "title_en"]
                if updated == current:
                    continue
                payload = _json.dumps(updated)
                if is_postgres:
                    await conn.execute(text(
                        "UPDATE channels SET required_metadata_fields = "
                        "CAST(:val AS JSONB) WHERE id = :id"
                    ), {"val": payload, "id": row.id})
                else:
                    await conn.execute(text(
                        "UPDATE channels SET required_metadata_fields = :val "
                        "WHERE id = :id"
                    ), {"val": payload, "id": row.id})
                changed += 1
            await conn.execute(text(
                "UPDATE app_settings SET value = 'done' WHERE key = :key"
            ), {"key": sentinel})
            if changed:
                logger.info("[migrate] removed legacy title_en requirement from %d channels", changed)

    # ── channels.required_metadata_fields per-season works convergence ─────
    # 作品单季化：``season`` 从形态必填退役为可选（季号由作品身份承载），
    # ``absolute_episode``/``episode_confidence`` 两键退役出目录。存量频道
    # 声明里的这三个键一次性移除（否则 season 会因 add-only 策略永远锁住
    # TV 行，另两键则不再通过目录校验）。哨兵保证只跑一次：用户此后可以
    # 在 add-only 策略下显式重新加入 ``season``。
    async with _best_effort(conn, "channels.required_fields per-season convergence"):
        import json as _json

        sentinel = "required_fields_per_season_v1"
        retired = {"season", "absolute_episode", "episode_confidence"}
        if is_turso:
            await conn.execute(text(
                "INSERT OR IGNORE INTO app_settings(key, value) "
                "VALUES (:key, 'pending')"
            ), {"key": sentinel})
        elif is_postgres:
            await conn.execute(text(
                "INSERT INTO app_settings(key, value) VALUES (:key, 'pending') "
                "ON CONFLICT (key) DO NOTHING"
            ), {"key": sentinel})
        marker = (await conn.execute(text(
            "SELECT value FROM app_settings WHERE key = :key"
        ), {"key": sentinel})).scalar_one_or_none()
        if marker == "pending":
            rows = await conn.execute(text(
                "SELECT id, required_metadata_fields FROM channels"
            ))
            changed = 0
            for row in rows:
                raw = row.required_metadata_fields
                if isinstance(raw, str):
                    try:
                        current = _json.loads(raw)
                    except ValueError:
                        current = []
                else:
                    current = list(raw or [])
                updated = [key for key in current if key not in retired]
                if updated == current:
                    continue
                payload = _json.dumps(updated)
                if is_postgres:
                    await conn.execute(text(
                        "UPDATE channels SET required_metadata_fields = "
                        "CAST(:val AS JSONB) WHERE id = :id"
                    ), {"val": payload, "id": row.id})
                else:
                    await conn.execute(text(
                        "UPDATE channels SET required_metadata_fields = :val "
                        "WHERE id = :id"
                    ), {"val": payload, "id": row.id})
                changed += 1
            await conn.execute(text(
                "UPDATE app_settings SET value = 'done' WHERE key = :key"
            ), {"key": sentinel})
            if changed:
                logger.info(
                    "[migrate] removed retired season/episode keys from %d channels",
                    changed,
                )

    async with _best_effort(conn, "channels.required_metadata_fields baseline"):
        import json as _json

        from app.services.required_fields import normalize_required_fields

        rows = await conn.execute(
            text("SELECT id, required_metadata_fields FROM channels")
        )
        updated = 0
        for row in rows:
            raw = row.required_metadata_fields
            if isinstance(raw, str):
                try:
                    current = _json.loads(raw)
                except ValueError:
                    current = []
            else:
                current = list(raw or [])
            normalized = normalize_required_fields(current)
            if normalized == current:
                continue
            payload = _json.dumps(normalized)
            if is_postgres:
                await conn.execute(
                    text(
                        "UPDATE channels SET required_metadata_fields = "
                        "CAST(:val AS JSONB) WHERE id = :id"
                    ),
                    {"val": payload, "id": row.id},
                )
            else:
                await conn.execute(
                    text(
                        "UPDATE channels SET required_metadata_fields = :val "
                        "WHERE id = :id"
                    ),
                    {"val": payload, "id": row.id},
                )
            updated += 1
        if updated:
            logger.info(
                "[migrate] converged %d channel required_metadata_fields rows "
                "to the locked baseline",
                updated,
            )

    # ── channels.required_metadata_fields NOT NULL (PostgreSQL) ──────
    # The convergence above guarantees no NULL rows remain, so the column
    # can be hardened to match the "mandatory, add-only" contract. Turso
    # cannot alter nullability in place; its table rebuild lives in
    # upgrade_sqlite_table_invariants (channels is a parent table — DROP
    # TABLE under FK enforcement would cascade-delete child rows).
    if is_postgres:
        async with _best_effort(conn, "channels.required_metadata_fields NOT NULL"):
            nullable = (await conn.execute(text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'channels' "
                "AND column_name = 'required_metadata_fields'"
            ))).scalar()
            if nullable == "YES":
                nulls = (await conn.execute(text(
                    "SELECT COUNT(*) FROM channels "
                    "WHERE required_metadata_fields IS NULL"
                ))).scalar_one()
                if nulls:
                    logger.warning(
                        "[migrate] skipped channels.required_metadata_fields "
                        "NOT NULL: %d NULL rows remain",
                        nulls,
                    )
                else:
                    await conn.execute(text(
                        "ALTER TABLE channels "
                        "ALTER COLUMN required_metadata_fields SET NOT NULL"
                    ))
                    logger.info(
                        "[migrate] channels.required_metadata_fields set NOT NULL"
                    )

    # ── one-time weak audio classification repair ───────────────────────
    # FLAC/ALAC are codecs, not proof that a release is an AudioWork. Clear
    # stub music links when the torrent enrichment already proves a numbered
    # multi-video batch; the normal metadata backfill will relink it as TV.
    async with _best_effort(conn, "weak audio classification repair"):
        sentinel = "weak_audio_stub_reclassify_v1"
        if is_turso:
            await conn.execute(text(
                "INSERT OR IGNORE INTO app_settings(key, value) "
                "VALUES (:key, 'pending')"
            ), {"key": sentinel})
        elif is_postgres:
            await conn.execute(text(
                "INSERT INTO app_settings(key, value) VALUES (:key, 'pending') "
                "ON CONFLICT (key) DO NOTHING"
            ), {"key": sentinel})
        marker = (await conn.execute(text(
            "SELECT value FROM app_settings WHERE key = :key"
        ), {"key": sentinel})).scalar_one_or_none()
        if marker == "pending":
            rows = await conn.execute(text(
                "SELECT fr.id, fr.audio_work_id "
                "FROM file_resources fr JOIN audio_works aw "
                "ON aw.id = fr.audio_work_id "
                "WHERE aw.external_source = 'stub' "
                "AND aw.content_type = 'music' "
                "AND fr.is_batch = TRUE "
                "AND (SELECT COUNT(*) FROM resource_file_assignments rfa "
                "     WHERE rfa.resource_id = fr.id "
                "       AND rfa.episode_start IS NOT NULL "
                "       AND rfa.episode_end IS NOT NULL) >= 2"
            ))
            repaired = 0
            for row in rows:
                await conn.execute(text(
                    "UPDATE file_resources SET audio_work_id = NULL, "
                    "metadata_matched_at = NULL, metadata_attempts = 0, "
                    "last_metadata_attempt_at = NULL, metadata_failure_type = NULL "
                    "WHERE id = :id"
                ), {"id": row.id})
                repaired += 1
            await conn.execute(text(
                "DELETE FROM audio_works WHERE external_source = 'stub' "
                "AND content_type = 'music' "
                "AND NOT EXISTS (SELECT 1 FROM file_resources fr "
                "                WHERE fr.audio_work_id = audio_works.id)"
            ))
            await conn.execute(text(
                "UPDATE app_settings SET value = 'done' WHERE key = :key"
            ), {"key": sentinel})
            if repaired:
                logger.info("[migrate] reset %d weak audio-linked resources for TV relinking", repaired)

    # ── one-time single-season batch coverage sync ─────────────────────
    # Older association saves updated only file-level seasons. Converge the
    # resource-level flat coverage fields used by Channel confirmation and
    # Agent dedup whenever assignments prove exactly one season.
    async with _best_effort(conn, "single-season batch coverage sync"):
        sentinel = "single_season_batch_coverage_sync_v1"
        if is_turso:
            await conn.execute(text(
                "INSERT OR IGNORE INTO app_settings(key, value) "
                "VALUES (:key, 'pending')"
            ), {"key": sentinel})
        elif is_postgres:
            await conn.execute(text(
                "INSERT INTO app_settings(key, value) VALUES (:key, 'pending') "
                "ON CONFLICT (key) DO NOTHING"
            ), {"key": sentinel})
        marker = (await conn.execute(text(
            "SELECT value FROM app_settings WHERE key = :key"
        ), {"key": sentinel})).scalar_one_or_none()
        if marker == "pending":
            result = await conn.execute(text(
                "UPDATE file_resources SET "
                "season = (SELECT MIN(rfa.season) FROM resource_file_assignments rfa "
                "          WHERE rfa.resource_id = file_resources.id), "
                "episode_start = (SELECT MIN(rfa.episode_start) "
                "                 FROM resource_file_assignments rfa "
                "                 WHERE rfa.resource_id = file_resources.id), "
                "episode_end = (SELECT MAX(rfa.episode_end) "
                "               FROM resource_file_assignments rfa "
                "               WHERE rfa.resource_id = file_resources.id) "
                "WHERE is_batch = TRUE AND batch_scope = 'season' "
                "AND series_id IS NOT NULL "
                "AND (SELECT COUNT(DISTINCT rfa.season) "
                "     FROM resource_file_assignments rfa "
                "     WHERE rfa.resource_id = file_resources.id "
                "       AND rfa.season IS NOT NULL) = 1"
            ))
            await conn.execute(text(
                "UPDATE app_settings SET value = 'done' WHERE key = :key"
            ), {"key": sentinel})
            if result.rowcount:
                logger.info(
                    "[migrate] synchronized flat coverage for %d single-season batches",
                    result.rowcount,
                )

    # ── downloader_type enum widening ────────────────────────────────────
    # Older PostgreSQL DBs may have a native enum restricting
    # ``downloader_instances.type`` to just ``'transmission'``. We now allow
    # ``'mock'`` as well (and the column has been widened to a plain String
    # in the ORM). Turso databases use a plain VARCHAR + CHECK from the start.
    # Databases created after the widening never had the enum at all, so
    # probe pg_type first instead of relying on the tolerated failure.
    async with _best_effort(conn, "downloader_type widening"):
        if is_postgres:
            has_enum = (await conn.execute(text(
                "SELECT 1 FROM pg_type WHERE typname = 'downloader_type'"
            ))).scalar()
            if has_enum:
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
            # environments. Each failure MUST be contained in its own
            # savepoint: swallowing an error without one aborts the whole
            # surrounding transaction and every later migration step with it.
            async with _best_effort(conn, "download_tasks.agent_id fk swap"):
                await conn.execute(text(
                    "ALTER TABLE download_tasks DROP CONSTRAINT IF EXISTS download_tasks_agent_id_fkey"
                ))
                await conn.execute(text(
                    "ALTER TABLE download_tasks "
                    "ADD CONSTRAINT download_tasks_agent_id_fkey "
                    "FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE SET NULL"
                ))

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
            f"  {now_sql}"
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

    # ── one-time stale "ambiguous" episode-confidence cleanup ────────────
    # The episode/season question only exists for single-episode tv resources,
    # but earlier versions could leave the flag stuck on resources the user
    # had already reclassified: marking a resource as 合集 without touching
    # episode fields, relinking to a movie, or flipping the work's
    # content_type away from tv all left ``episode_confidence='ambiguous'``
    # untouched, pinning the resource on the dashboard 待确认 list forever.
    # Clear those once: batches become "manual" (a human made the call),
    # movie-linked / non-tv-linked rows drop the flag (NULL = no episode
    # assessment applies).
    async with _best_effort(conn, "stale ambiguous cleanup"):
        sentinel = "ambiguous_stale_clear"
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
            r1 = await conn.execute(text(
                "UPDATE file_resources SET episode_confidence = 'manual' "
                "WHERE is_batch AND episode_confidence = 'ambiguous'"
            ))
            r2 = await conn.execute(text(
                "UPDATE file_resources SET episode_confidence = NULL "
                "WHERE movie_id IS NOT NULL AND episode_confidence = 'ambiguous'"
            ))
            r3 = await conn.execute(text(
                "UPDATE file_resources SET episode_confidence = NULL "
                "WHERE episode_confidence = 'ambiguous' AND series_id IN "
                "(SELECT id FROM tv_series WHERE content_type <> 'tv')"
            ))
            await conn.execute(text(
                f"UPDATE app_settings SET value = 'done' WHERE key = '{sentinel}'"
            ))
            logger.info(
                "[migrate] cleared stale ambiguous flags: %s batch, %s movie-linked, "
                "%s non-tv-series-linked",
                getattr(r1, "rowcount", "?"), getattr(r2, "rowcount", "?"),
                getattr(r3, "rowcount", "?"),
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
                    f"{now_sql}, {now_sql})"
                ), {"id": str(_uuid.uuid4()), "name": "Plex", "url": plex_url,
                    "token": plex_token, "enabled": True})
                logger.info(
                    "[migrate] converted global PLEX_URL/PLEX_TOKEN into a "
                    "media_server_instances row"
                )

    # ── batch_scope 'movies' 枚举扩展：存量改写 ──────────────────────────
    # franchise 原本同时覆盖「纯电影包」与「TV+Movie 混合包」。引入
    # 'movies' scope 后，把 collection 挂载成员全部为 movie 的存量
    # franchise 行一次性改写为 'movies'；无法离线重析文件清单的行保持
    # franchise（长期兼容的合法值），由后续向导保存或重新解析自然收敛。
    # 幂等：只触碰仍为 franchise 的行。
    async with _best_effort(conn, "batch_scope movies rewrite"):
        res = await conn.execute(text(
            "UPDATE file_resources SET batch_scope = 'movies' "
            "WHERE batch_scope = 'franchise' AND collection_id IS NOT NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM tv_series s WHERE s.collection_id = file_resources.collection_id"
            ") AND EXISTS ("
            "  SELECT 1 FROM movies m WHERE m.collection_id = file_resources.collection_id)"
        ))
        if getattr(res, "rowcount", 0):
            logger.info(
                "[migrate] rewrote %d movie-only franchise rows to batch_scope='movies'",
                res.rowcount,
            )

    # ── JSON → JSONB convergence (PostgreSQL) ────────────────────────
    # ORM JSON columns compile to JSONB on PostgreSQL (the
    # app.models.db_types.json_column variant) and the ADD COLUMN path above
    # already emits JSONB, but databases created by older create_all runs
    # carry plain ``json`` columns. Align them here; the json → jsonb cast
    # is lossless. metadata_cache is excluded (its model still declares
    # plain JSON). Idempotent: converted columns report data_type 'jsonb'
    # and drop out of the probe.
    if is_postgres:
        json_cols = (await conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND data_type = 'json' "
            "AND table_name <> 'metadata_cache' "
            "AND table_name = ANY(CAST(:tables AS text[])) "
            "ORDER BY table_name, column_name"
        ), {"tables": sorted(Base.metadata.tables)})).fetchall()
        for table, column in json_cols:
            async with _best_effort(conn, f"json→jsonb {table}.{column}"):
                await conn.execute(text(
                    f"ALTER TABLE {table} ALTER COLUMN {column} TYPE JSONB"
                ))
                logger.info("[migrate] converted %s.%s to JSONB", table, column)

    # ── light-migration ledger flush ───────────────────────────────────
    # Last step on purpose (DML after all DDL; see _flush_migration_ledger).
    await _flush_migration_ledger(conn, applied_blocks)
    conn.info.pop(_LEDGER_INFO_KEY, None)


async def upgrade_sqlite_table_invariants(engine) -> None:
    """Embedded-SQLite (Turso/aiosqlite) structural rebuilds that
    ``_apply_light_migrations`` cannot host.

    Two driver constraints force a separate phase: DDL is rejected once a
    ``BEGIN CONCURRENT`` transaction has seen DML, and rebuilding
    ``channels`` (a parent table) under FK enforcement would cascade-delete
    child rows through DROP TABLE's implicit DELETE. This phase owns a fresh
    ordinary transaction (explicit ``BEGIN``) and suspends FK enforcement
    only around the parent-table rebuild — the same pattern as
    ``app.services.schema_foreign_keys.repair_turso_foreign_keys``. Each
    item probes first and is idempotent; fresh databases are fast no-ops.
    """
    async with engine.begin() as conn:
        await conn.execute(text("BEGIN"))

        # ── episodes composite FK ────────────────────────────────────
        # SQLite adds FKs only at CREATE TABLE, so the (series_id, season)
        # → tv_series(id, season_number) invariant needs a table rebuild.
        # Fixable disagreements are re-tagged to the parent season first;
        # unfixable collisions (pre-split legacy multi-season rows) keep the
        # legacy shape with a warning until the season-split migration runs.
        fks = (await conn.execute(
            text("PRAGMA foreign_key_list(episodes)")
        )).fetchall()
        if not any(row[2] == "tv_series" and row[3] == "season" for row in fks):
            try:
                await conn.execute(text(_EPISODE_SEASON_RETAG_SQL))
                violations = (await conn.execute(
                    text(_EPISODE_SEASON_VIOLATIONS_SQL)
                )).fetchall()
                if violations:
                    logger.warning(
                        "[migrate] skipped episodes composite FK: rows %s violate "
                        "season == season_number; finish the season-split "
                        "migration and restart",
                        [v[0] for v in violations],
                    )
                else:
                    # Parent unique target required by the composite FK. The
                    # light migration normally creates it first; repeat here
                    # (coverage-probed) so a skipped/failed earlier run cannot
                    # break the rebuild.
                    if not await _sqlite_unique_covers(
                        conn, "tv_series", ("id", "season_number")
                    ):
                        await conn.execute(text(
                            "CREATE UNIQUE INDEX IF NOT EXISTS "
                            "uq_tv_series_id_season_number "
                            "ON tv_series (id, season_number)"
                        ))
                    await _rebuild_sqlite_table(conn, "episodes")
                    logger.info(
                        "[migrate] rebuilt episodes with composite FK "
                        "(series_id, season)"
                    )
            except Exception:
                logger.exception("[migrate] episodes composite FK rebuild skipped")

        # ── channels.required_metadata_fields NOT NULL ───────────────
        # The light-migration baseline convergence already rewrote NULL /
        # partial rows (NULL backfill repeated here defensively); the rebuild
        # then hardens the column. Legacy dead columns no longer on the model
        # (parser_type, title_extraction_*) are dropped by the rebuild.
        info = (await conn.execute(text("PRAGMA table_info(channels)"))).fetchall()
        rf_row = next((row for row in info if row[1] == "required_metadata_fields"), None)
        if rf_row is not None and not rf_row[3]:
            try:
                nulls = (await conn.execute(text(
                    "SELECT COUNT(*) FROM channels "
                    "WHERE required_metadata_fields IS NULL"
                ))).scalar_one()
                if nulls:
                    import json as _json

                    from app.services.required_fields import normalize_required_fields

                    baseline = _json.dumps(normalize_required_fields([]))
                    await conn.execute(text(
                        "UPDATE channels SET required_metadata_fields = :baseline "
                        "WHERE required_metadata_fields IS NULL"
                    ), {"baseline": baseline})
                    logger.info(
                        "[migrate] backfilled %d NULL "
                        "channels.required_metadata_fields rows", nulls,
                    )
                await conn.execute(text("PRAGMA foreign_keys=OFF"))
                try:
                    if await conn.scalar(text("PRAGMA foreign_keys")) != 0:
                        raise RuntimeError(
                            "Cannot suspend foreign keys for channels rebuild"
                        )
                    await _rebuild_sqlite_table(conn, "channels")
                finally:
                    await conn.execute(text("PRAGMA foreign_keys=ON"))
                logger.info(
                    "[migrate] rebuilt channels with NOT NULL "
                    "required_metadata_fields"
                )
            except Exception:
                logger.exception("[migrate] channels NOT NULL rebuild skipped")
