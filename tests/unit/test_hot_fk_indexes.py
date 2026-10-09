"""P1-D5 hot-FK index regression tests.

The indexes below were verified by EXPLAIN at synthetic 16k-row scale on both
Turso and PostgreSQL (docs/plans/p0-and-backlog/V28-HOT-FK-INDEXES.md):

- Fresh schema (``create_all``) exposes them from the model ``__table_args__``.
- A pre-upgrade database (indexes absent) regains them via
  ``_apply_light_migrations``; re-running the migration is idempotent.
- The migration DDL names and the model ``Index`` definitions cannot drift:
  the dropped model index must come back under its model name.
"""

from sqlalchemy import text

import app.database as db_mod
from app.database import Base, _apply_light_migrations

# index name -> (table, expected columns in order)
EXPECTED_INDEXES = {
    "ix_file_resources_series_id": ("file_resources", ("series_id",)),
    "ix_file_resources_movie_id": ("file_resources", ("movie_id",)),
    "ix_file_resources_audio_work_id": ("file_resources", ("audio_work_id",)),
    "ix_file_resources_collection_id": ("file_resources", ("collection_id",)),
    "ix_agent_works_agent_id": ("agent_works", ("agent_id",)),
    "ix_agent_works_series_id": ("agent_works", ("series_id",)),
    "ix_agent_works_movie_id": ("agent_works", ("movie_id",)),
    "ix_pending_decisions_series_id": ("pending_decisions", ("series_id",)),
    "ix_pending_decisions_movie_id": ("pending_decisions", ("movie_id",)),
    "ix_download_tasks_file_resource_id": ("download_tasks", ("file_resource_id",)),
    "ix_agent_runs_agent_started": ("agent_runs", ("agent_id", "started_at")),
    "ix_webhook_deliveries_status_created": ("webhook_deliveries", ("status", "created_at")),
}


async def _index_columns(conn, table: str) -> dict[str, tuple[str, ...]]:
    rows = (await conn.execute(text(f"PRAGMA index_list({table})"))).fetchall()
    out = {}
    for row in rows:
        cols = (
            await conn.execute(text(f"PRAGMA index_info({row[1]})"))
        ).fetchall()
        out[row[1]] = tuple(r[2] for r in cols)
    return out


async def _assert_indexes(conn) -> None:
    by_table: dict[str, dict[str, tuple[str, ...]]] = {}
    for name, (table, columns) in EXPECTED_INDEXES.items():
        if table not in by_table:
            by_table[table] = await _index_columns(conn, table)
        assert name in by_table[table], f"{name} missing on {table}"
        assert by_table[table][name] == columns, (
            f"{name} columns {by_table[table][name]} != {columns}"
        )


async def test_hot_fk_indexes_exist_on_fresh_schema(db_engine):
    async with db_engine.begin() as conn:
        await _assert_indexes(conn)


def test_hot_fk_indexes_defined_in_model_metadata():
    """The expected names live on the model tables, so ``create_all`` produces
    them and rebuild-based migrations (``table.indexes``) preserve them."""
    for name, (table, _columns) in EXPECTED_INDEXES.items():
        index_names = {ix.name for ix in Base.metadata.tables[table].indexes}
        assert name in index_names, f"{name} not defined on model table {table}"


async def test_light_migration_recreates_dropped_hot_fk_indexes(db_engine):
    """Old-shape database (indexes absent) → light migration → indexes exist;
    a second run is a no-op."""
    async with db_engine.begin() as conn:
        for name in EXPECTED_INDEXES:
            await conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
        for table in {t for t, _ in EXPECTED_INDEXES.values()}:
            present = await _index_columns(conn, table)
            for name in EXPECTED_INDEXES:
                assert name not in present

        await _apply_light_migrations(conn)
        await _assert_indexes(conn)

        # Idempotent re-run: same indexes, no duplicates.
        await _apply_light_migrations(conn)
        await _assert_indexes(conn)
        for table in {t for t, _ in EXPECTED_INDEXES.values()}:
            present = await _index_columns(conn, table)
            expected_on_table = {
                n for n, (t, _c) in EXPECTED_INDEXES.items() if t == table
            }
            assert expected_on_table <= set(present)


async def test_light_migration_skips_hot_fk_index_on_missing_column(
    db_engine, monkeypatch
):
    """Legacy pre-AudioWork shape: ``file_resources`` has no ``audio_work_id``
    column (it arrives via a later migration/rebuild path). The migration must
    not abort on CREATE INDEX over the missing column — it skips that one
    index, still creates the rest, and a re-run after the column arrives fills
    in the skipped index (idempotent convergence)."""
    # Keep the column absent for the first run: simulate its arrival via a
    # later rebuild path rather than the light column additions.
    monkeypatch.setattr(
        db_mod,
        "_LIGHT_COLUMN_ADDITIONS",
        tuple(
            entry
            for entry in db_mod._LIGHT_COLUMN_ADDITIONS
            if entry[:2] != ("file_resources", "audio_work_id")
        ),
    )

    # Rebuild file_resources without audio_work_id. SQLite cannot DROP a
    # column referenced by the XOR CHECK constraint, so recreate the table
    # with the legacy column set (CREATE TABLE AS SELECT drops the
    # constraints, matching the pre-guard legacy shape). The resource parent
    # guards must go first: Turso validates triggers on other tables that
    # reference the temporarily absent parent during RENAME.
    from app.services.resource_parent_guard import drop_resource_parent_guards

    async with db_engine.begin() as conn:
        # CREATE TABLE AS SELECT is DML-bearing; an ordinary (exclusive)
        # transaction is required for the subsequent DROP/RENAME DDL — same
        # pattern as the schema-repair transaction in create_tables.
        await conn.execute(text("BEGIN"))
        await conn.run_sync(drop_resource_parent_guards)
        legacy_columns = [
            row[1]
            for row in (
                await conn.execute(text("PRAGMA table_info(file_resources)"))
            ).fetchall()
            if row[1] != "audio_work_id"
        ]
        assert len(legacy_columns) > 1
        await conn.execute(text(
            "CREATE TABLE legacy_file_resources AS "
            f"SELECT {', '.join(legacy_columns)} FROM file_resources"
        ))
        await conn.execute(text("DROP TABLE file_resources"))
        await conn.execute(text(
            "ALTER TABLE legacy_file_resources RENAME TO file_resources"
        ))

    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)

        present = await _index_columns(conn, "file_resources")
        assert "ix_file_resources_audio_work_id" not in present
        for name, (table, columns) in EXPECTED_INDEXES.items():
            if name == "ix_file_resources_audio_work_id":
                continue
            by_table = (
                present if table == "file_resources"
                else await _index_columns(conn, table)
            )
            assert name in by_table, f"{name} missing on legacy {table}"
            assert by_table[name] == columns

    # The column arrives (later rebuild path); the next startup migration
    # re-probes and fills in the skipped index. The ALTER needs its own
    # transaction: the migration above already saw DML, and MVCC CONCURRENT
    # transactions reject DDL after DML.
    async with db_engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE file_resources ADD COLUMN audio_work_id "
            "VARCHAR(36) REFERENCES audio_works(id) ON DELETE SET NULL"
        ))

    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)
        await _assert_indexes(conn)
