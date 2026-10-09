"""Regression tests for the light-migration ledger (``schema_migrations``)
and the single-source ``_LIGHT_COLUMN_ADDITIONS`` guardrail.

Design contract under test (see docs/design/db-migration.md):

- every named block of ``_apply_light_migrations`` that runs to completion is
  recorded once in ``schema_migrations`` (name PK, applied_at UTC);
- the ledger is observational only — probes remain the correctness authority,
  so a ledger row claiming an applied change must never suppress the probe
  (damaged/restored databases self-heal);
- failed best-effort blocks are NOT recorded;
- the column additions live in one immutable module-level tuple — no runtime
  append can drift away from the reviewed list again.
"""

import inspect
from types import SimpleNamespace

from sqlalchemy import MetaData, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine

import app.database as db_mod
import app.models.schema_migration  # noqa: F401  (register the ledger model)
from app.database import (
    _LIGHT_COLUMN_ADDITIONS,
    Base,
    _apply_light_migrations,
    _best_effort,
    normalize_database_url,
)
from app.models.schema_migration import SchemaMigration


async def _ledger_names(conn) -> set[str]:
    rows = (await conn.execute(select(SchemaMigration.name))).all()
    return {row[0] for row in rows}


async def test_ledger_populated_and_idempotent(db_engine):
    """A full run records every block once; a second startup changes nothing."""
    for _ in range(2):  # two "startups" — the rerun must be a no-op
        async with db_engine.begin() as conn:
            await _apply_light_migrations(conn)

    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                select(SchemaMigration.name, SchemaMigration.applied_at)
            )
        ).all()
    names = {row[0] for row in rows}
    # PK uniqueness + INSERT-IF-MISSING: no duplicates after two runs.
    assert len(names) == len(rows)
    assert all(row[1] is not None for row in rows)
    # 74 column additions + the named blocks.
    assert len(names) >= 100
    expected = {
        # delegated mandatory step
        "resource parent guards",
        # column additions (probe-confirmed present)
        "add column file_resources.is_batch",
        "add column webhook_deliveries.attempt_token",
        "add column tv_series.season_number",
        # best-effort schema blocks
        "tv_series (id, season_number) unique",
        "unique constraint backfill",
        "uq_tv_series_collection_season",
        # sentinel-gated one-time data migrations
        "channels.title_cn compatibility",
        "channels.required_fields per-season convergence",
        "non_work reset",
        "stale ambiguous cleanup",
        # convergence updates
        "channels.metadata_source convergence",
        "batch_scope movies rewrite",
        "organize configuration singleton",
    }
    assert expected <= names


async def test_ledger_row_does_not_suppress_probe(tmp_path):
    """Anomalous database: the ledger claims a column that is actually missing.

    The probe (not the ledger) is the correctness authority, so the column
    must still be added — the ledger never short-circuits a probe.
    """
    legacy = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(legacy)
    resources = legacy.tables["file_resources"]
    resources._columns.remove(resources.c.magnet_resolve_status)
    engine = create_async_engine(
        normalize_database_url(f"sqlite+aioturso:///{tmp_path / 'legacy.db'}")
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(legacy.create_all)
            await conn.execute(text("PRAGMA journal_mode='mvcc'"))
            cols = (await conn.execute(
                text("PRAGMA table_info(file_resources)")
            )).fetchall()
            assert "magnet_resolve_status" not in {row[1] for row in cols}
            # Ledger lies: claims the column was already applied.
            await conn.execute(text(
                "INSERT INTO schema_migrations (name) VALUES "
                "('add column file_resources.magnet_resolve_status')"
            ))
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)
        async with engine.connect() as conn:
            cols = (await conn.execute(
                text("PRAGMA table_info(file_resources)")
            )).fetchall()
            assert "magnet_resolve_status" in {row[1] for row in cols}
            assert "add column file_resources.magnet_resolve_status" in (
                await _ledger_names(conn)
            )
    finally:
        await engine.dispose()


async def test_ledger_table_created_for_legacy_schema(tmp_path):
    """Direct callers whose schema predates the model import get the table."""
    legacy = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name != "schema_migrations":
            table.to_metadata(legacy)
    engine = create_async_engine(
        normalize_database_url(f"sqlite+aioturso:///{tmp_path / 'pre-ledger.db'}")
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(legacy.create_all)
            await conn.execute(text("PRAGMA journal_mode='mvcc'"))
            assert not await conn.run_sync(
                lambda sync: sa_inspect(sync).has_table("schema_migrations")
            )
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)
        async with engine.connect() as conn:
            assert await conn.run_sync(
                lambda sync: sa_inspect(sync).has_table("schema_migrations")
            )
            assert len(await _ledger_names(conn)) >= 100
    finally:
        await engine.dispose()


async def test_failed_block_is_not_recorded(db_engine):
    """A best-effort block that fails must be absent from the ledger while
    everything else is still recorded and startup does not fail."""
    async with db_engine.begin() as conn:
        await conn.execute(text("DROP TABLE metadata_cache"))
    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)
    async with db_engine.connect() as conn:
        names = await _ledger_names(conn)
    # The metadata_cache DELETE and its column addition both failed.
    assert "global metadata auto-refresh settings cleanup" not in names
    assert "add column metadata_cache.generation" not in names
    # Unrelated blocks are unaffected.
    assert "channels.metadata_source convergence" in names
    assert "add column file_resources.is_batch" in names


def test_additions_single_source_of_truth():
    """The column additions are one immutable module-level tuple — a stray
    ``additions.append(...)`` (the historical drift bug) is impossible."""
    assert isinstance(_LIGHT_COLUMN_ADDITIONS, tuple)
    keys = [(table, column) for table, column, _ in _LIGHT_COLUMN_ADDITIONS]
    assert len(keys) == len(set(keys)), "duplicate (table, column) entry"
    # The historically stray entry lives in the main list now.
    assert ("webhook_deliveries", "attempt_token") in keys
    assert len(_LIGHT_COLUMN_ADDITIONS) == 74
    # The migration body consumes the constant; no runtime mutation anywhere.
    body = inspect.getsource(db_mod._apply_light_migrations)
    assert "_LIGHT_COLUMN_ADDITIONS" in body
    module_source = inspect.getsource(db_mod)
    assert "additions.append((" not in module_source


async def test_best_effort_hook_records_only_success():
    """The ledger hook: completed bodies are recorded, failed ones are not."""

    class _Savepoint:
        async def start(self):
            return self

        async def rollback(self):
            return None

        async def commit(self):
            return None

    class _Conn:
        def __init__(self):
            self.info: dict = {}
            self.dialect = SimpleNamespace(name="sqlite")

        def begin_nested(self):
            return _Savepoint()

    conn = _Conn()
    conn.info[db_mod._LEDGER_INFO_KEY] = blocks = []
    async with _best_effort(conn, "ok block"):
        pass
    async with _best_effort(conn, "failing block"):
        raise RuntimeError("boom")
    assert blocks == ["ok block"]

    # Without the ledger key (other _best_effort callers, e.g. pg_trgm),
    # the hook is a no-op.
    other = _Conn()
    async with _best_effort(other, "untracked"):
        pass
    assert other.info == {}
