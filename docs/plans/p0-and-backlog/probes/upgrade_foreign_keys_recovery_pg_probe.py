"""PG FK repair: dirty-data rollback and concurrent real startup processes."""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import insert, inspect, select, text
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host in {"127.0.0.1", "localhost"}
assert (url.username, url.password, url.database) == ("organize_test",) * 3

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.services.schema_foreign_keys import _COLUMNS  # noqa: E402


async def missing_count(conn):
    catalog = await conn.run_sync(
        lambda sync: {table: inspect(sync).get_foreign_keys(table) for table in dict.fromkeys(t for t, _ in _COLUMNS)}
    )
    return sum(not any(f["constrained_columns"] == [column] for f in catalog[table]) for table, column in _COLUMNS)


async def main():
    tables = database.Base.metadata.tables
    processes = []
    try:
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)
            await conn.run_sync(database.Base.metadata.create_all)
            catalog = await conn.run_sync(
                lambda sync: {
                    table: inspect(sync).get_foreign_keys(table) for table in dict.fromkeys(t for t, _ in _COLUMNS)
                }
            )
            quote = conn.dialect.identifier_preparer.quote
            for table, column in _COLUMNS:
                for fk in catalog[table]:
                    if fk["constrained_columns"] == [column]:
                        await conn.execute(text(f"ALTER TABLE {quote(table)} DROP CONSTRAINT {quote(fk['name'])}"))
            work_id, parent_id = str(uuid.uuid4()), str(uuid.uuid4())
            await conn.execute(
                insert(tables["tv_series"]).values(
                    id=work_id,
                    collection_id=parent_id,
                    season_number=3,
                    title_cn="Synthetic protected orphan",
                    manually_edited_fields=["title_cn"],
                )
            )
            before = (await conn.execute(select(tables["tv_series"]))).all()
        try:
            await database.create_tables()
        except RuntimeError as exc:
            assert "orphan row IDs" in str(exc) and work_id in str(exc), str(exc)
        else:
            raise AssertionError("Dirty database startup unexpectedly succeeded")
        async with database.engine.begin() as conn:
            assert await missing_count(conn) == 7  # earlier repair ALTER must also roll back
            assert (await conn.execute(select(tables["tv_series"]))).all() == before
            # Repair this known synthetic parent association without changing the child.
            await conn.execute(
                insert(tables["work_collections"]).values(id=parent_id, title_cn="Synthetic restored parent")
            )
        async with database.engine.begin() as blocker:
            await blocker.execute(text("SELECT pg_advisory_xact_lock(72057594037927937)"))
            for _ in range(2):
                processes.append(
                    await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-c",
                        "import asyncio; from app.database import create_tables; asyncio.run(create_tables())",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                )

            async def observe_both():
                async with database.engine.connect() as conn:
                    while True:
                        # pg_stat_activity caches its snapshot within a transaction.
                        await conn.execute(text("SELECT pg_stat_clear_snapshot()"))
                        for process in processes:
                            if process.returncode is not None:
                                stdout, stderr = await process.communicate()
                                raise AssertionError((process.returncode, stdout.decode(), stderr.decode()))
                        count = await conn.scalar(
                            text("""
                            SELECT count(*) FROM pg_stat_activity
                            WHERE datname=current_database() AND pid<>pg_backend_pid()
                              AND query LIKE '%SELECT pg_advisory_xact_lock%'
                              AND cardinality(pg_blocking_pids(pid))>0
                        """)
                        )
                        if count >= 2:
                            return count
                        await asyncio.sleep(0.02)

            blocked = await asyncio.wait_for(observe_both(), 10)
        for process in processes:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
            assert process.returncode == 0, (stdout.decode(), stderr.decode())
        async with database.engine.connect() as conn:
            assert await missing_count(conn) == 0
            after = (await conn.execute(select(tables["tv_series"]))).one()
            assert (
                after.id,
                after.collection_id,
                after.season_number,
                after.title_cn,
                after.manually_edited_fields,
            ) == (
                work_id,
                parent_id,
                3,
                "Synthetic protected orphan",
                ["title_cn"],
            )
        result = {
            "dirty_startup_rejected": True,
            "all_seven_alters_rolled_back": True,
            "business_row_unchanged": True,
            "concurrent_startup_exit_codes": [p.returncode for p in processes],
            "blocked_startups_observed": blocked,
            "missing_foreign_keys_after_retry": 0,
        }
        Path(os.environ["RESULT_PATH"]).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        await database.engine.dispose()


asyncio.run(main())
