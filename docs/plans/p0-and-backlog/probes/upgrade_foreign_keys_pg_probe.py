"""Real PostgreSQL startup parity for seven lightweight-migration foreign keys.

Synthetic legacy schemas; restricted to an isolated local test database.
"""

import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host in {"127.0.0.1", "localhost"}
assert (url.username, url.password, url.database) == ("organize_test",) * 3

from app.services.schema_foreign_keys import _COLUMNS  # noqa: E402
from tests.unit.test_upgrade_foreign_keys import assert_fk_actions  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401


async def catalogs(conn):
    return await conn.run_sync(lambda sync: {table: inspect(sync).get_foreign_keys(table) for table, _ in _COLUMNS})


async def main():
    result = {}
    try:
        for mode in ["fresh", "missing-column", "existing-column"]:
            async with database.engine.begin() as conn:
                await conn.run_sync(database.Base.metadata.drop_all)
                await conn.run_sync(database.Base.metadata.create_all)
                actual = await catalogs(conn)
                quote = conn.dialect.identifier_preparer.quote
                for table, column in _COLUMNS:
                    if mode == "missing-column":
                        await conn.execute(text(f"ALTER TABLE {quote(table)} DROP COLUMN {quote(column)} CASCADE"))
                    elif mode == "existing-column":
                        for fk in actual[table]:
                            if fk["constrained_columns"] == [column]:
                                await conn.execute(
                                    text(f"ALTER TABLE {quote(table)} DROP CONSTRAINT {quote(fk['name'])}")
                                )
            await database.create_tables()
            await database.create_tables()
            async with database.engine.connect() as conn:
                actual = await catalogs(conn)
            checks = {}
            for table, column in _COLUMNS:
                (expected,) = database.Base.metadata.tables[table].c[column].foreign_keys
                checks[f"{table}.{column}"] = any(
                    fk["constrained_columns"] == [column]
                    and fk["referred_table"] == expected.column.table.name
                    and fk["referred_columns"] == [expected.column.name]
                    and fk["options"].get("ondelete", "NO ACTION") == (expected.ondelete or "NO ACTION")
                    for fk in actual[table]
                )
            for table, column in _COLUMNS:
                (fk,) = database.Base.metadata.tables[table].c[column].foreign_keys
                await assert_fk_actions(
                    database.engine, table, column, fk.column.table.name, fk.ondelete or "NO ACTION"
                )
            result[mode] = checks
        semantics = {}
        for keep_correct in [True, False]:
            async with database.engine.begin() as conn:
                if not keep_correct:
                    actual = await catalogs(conn)
                    for fk in actual["tv_series"]:
                        if fk["constrained_columns"] == ["collection_id"]:
                            name = conn.dialect.identifier_preparer.quote(fk["name"])
                            await conn.execute(text(f"ALTER TABLE tv_series DROP CONSTRAINT {name}"))
                await conn.execute(text(
                    "ALTER TABLE tv_series ADD CONSTRAINT synthetic_wrong_collection "
                    "FOREIGN KEY(collection_id) REFERENCES work_collections(id) ON DELETE CASCADE"
                ))
                before = (await conn.execute(text("SELECT * FROM tv_series ORDER BY id"))).all()
            try:
                await database.create_tables()
            except RuntimeError as exc:
                assert "Unexpected foreign key semantics for tv_series.collection_id" in str(exc), str(exc)
            else:
                raise AssertionError("Unexpected foreign key semantics accepted")
            async with database.engine.begin() as conn:
                assert (await conn.execute(text("SELECT * FROM tv_series ORDER BY id"))).all() == before
                await conn.execute(text("ALTER TABLE tv_series DROP CONSTRAINT synthetic_wrong_collection"))
            semantics["correct-and-wrong" if keep_correct else "wrong-only"] = True
        result["conflicting-semantics"] = semantics
        Path(os.environ["RESULT_PATH"]).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        assert all(ok for checks in result.values() for ok in checks.values()), result
    finally:
        await database.engine.dispose()


asyncio.run(main())
