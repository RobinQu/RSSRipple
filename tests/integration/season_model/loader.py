"""Load the production work-graph fixture into a test database via the ORM.

The fixture (``tests/fixtures/prod_works_v1.json``, produced by
``scripts/export_work_fixture.py``) stores rows as plain dicts: datetimes are
ISO strings, JSON columns are already-parsed dicts/lists, enums are plain
strings. Loading goes through the ORM models (never raw Core inserts) so the
``before_flush`` hooks fire and ``search_text`` / the ``fts_outbox`` queue are
maintained exactly as in production writes.

Rows are inserted in ``scripts.export_work_fixture.TABLE_ORDER`` (FK-safe
order) in a single transaction. Columns missing from the fixture (schema
added after the export, e.g. ``tv_series.season_number``) are simply omitted
so ORM/server defaults apply; keys unknown to the current models are dropped
defensively (the export reflects the production schema at capture time).

The loader is a generic tool — any test suite can ``load_fixture(db, data)``
on an empty database. ``assert_fixture_loaded`` cross-checks per-table row
counts against ``meta.counts`` so a silently dropped row fails loudly.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Date, DateTime, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.export_work_fixture import TABLE_ORDER

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "fixtures" / "prod_works_v1.json"
)


def load_fixture_data(path: Path = FIXTURE_PATH) -> dict:
    """Read the fixture JSON (``{"meta": ..., "tables": {name: [row]}}``)."""
    with open(path) as f:
        return json.load(f)


def _table_models() -> dict[str, type]:
    from app.database import Base

    return {m.local_table.name: m.class_ for m in Base.registry.mappers}


def _coerce_value(column, value: Any) -> Any:
    """Convert a fixture JSON value back to the column's Python type."""
    if value is None:
        return None
    if isinstance(column.type, DateTime) and isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            # Models store naive UTC; normalize aware stamps defensively.
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    if isinstance(column.type, Date) and isinstance(value, str):
        return date.fromisoformat(value)
    if isinstance(column.type, JSON) and isinstance(value, str):
        # Defensive: the exporter emits parsed JSON, but a reflected TEXT
        # column on the source DB would surface as a JSON string instead.
        return json.loads(value)
    return value


def _coerce_row(table, row: dict) -> dict:
    return {
        column.name: _coerce_value(column, row[column.name])
        for column in table.columns
        if column.name in row
    }


async def load_fixture(engine, data: dict) -> dict[str, int]:
    """Insert every fixture row through the ORM in FK-safe order.

    One flush per table keeps hook bookkeeping per table; a single commit at
    the end keeps the whole load atomic. Returns per-table inserted counts.

    FK enforcement is disabled on the loading connection for the duration of
    the load: the fixture is a *subgraph* export (``TABLE_ORDER`` covers the
    work graph only — e.g. ``downloader_instances.volume_id`` may reference a
    ``storage_volumes`` row that was never exported), and SQLite/Turso
    validate FKs at statement time, so subgraph edges to non-exported tables
    must not block the load. FK checks are re-enabled afterwards.
    """
    from app.database import Base

    models = _table_models()
    counts: dict[str, int] = {}
    async with engine.connect() as conn:
        # PRAGMA foreign_keys is a no-op inside a transaction — toggle it on
        # the raw connection before the ORM session begins one.
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        async with AsyncSession(bind=conn, expire_on_commit=False) as db:
            for name in TABLE_ORDER:
                rows = data["tables"].get(name, [])
                model = models.get(name)
                if model is None:
                    raise AssertionError(f"fixture table {name!r} has no ORM model")
                table = Base.metadata.tables[name]
                db.add_all([model(**_coerce_row(table, row)) for row in rows])
                await db.flush()
                counts[name] = len(rows)
            await db.commit()
        # An ORM session bound to an external connection does not COMMIT the
        # driver-level transaction under aioturso — commit the connection
        # explicitly before re-enabling FK checks.
        await conn.commit()
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    return counts


async def assert_fixture_loaded(db: AsyncSession, data: dict) -> None:
    """Assert live per-table row counts equal the fixture's ``meta.counts``."""
    models = _table_models()
    expected = data["meta"]["counts"]
    for name in TABLE_ORDER:
        actual = int(
            (
                await db.execute(select(func.count()).select_from(models[name]))
            ).scalar_one()
        )
        assert actual == expected[name], (
            f"fixture load dropped rows: {name} expected {expected[name]}, "
            f"got {actual}"
        )
