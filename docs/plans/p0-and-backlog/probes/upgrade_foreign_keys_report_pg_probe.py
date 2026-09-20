"""Actual PG preflight CLI: complete orphan export and no business mutations."""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import delete, insert, select, text
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host in {"127.0.0.1", "localhost"}
assert (url.username, url.password, url.database) == ("organize_test",) * 3

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401


async def main():
    table = database.Base.metadata.tables["movies"]
    report = Path(os.environ["REPORT_PATH"])
    try:
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)
            await conn.run_sync(database.Base.metadata.create_all)
            await conn.execute(text("ALTER TABLE movies DROP CONSTRAINT movies_collection_id_fkey"))
            values = [
                {
                    "id": str(uuid.uuid4()),
                    "collection_id": str(uuid.uuid4()),
                    "title_cn": f"Synthetic protected {n}",
                    "manually_edited_fields": ["title_cn"],
                }
                for n in range(103)
            ]
            await conn.execute(insert(table), values)
            before = (await conn.execute(select(table).order_by(table.c.id))).all()

        async def cli():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "scripts.verify_upgrade_foreign_keys",
                "--output",
                str(report),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            return process.returncode, stdout.decode(), stderr.decode()

        first = await cli()
        assert first[0] == 1, first
        rows = [json.loads(line) for line in report.read_text().splitlines()]
        assert len(rows) == 103
        assert {r["id"]: r["parent_id"] for r in rows} == {v["id"]: v["collection_id"] for v in values}
        assert all(
            (r["table"], r["column"], r["target"]) == ("movies", "collection_id", "work_collections.id") for r in rows
        )
        async with database.engine.begin() as conn:
            assert (await conn.execute(select(table).order_by(table.c.id))).all() == before
            await conn.execute(delete(table))  # fixture reset in this disposable database only
        second = await cli()
        assert second[0] == 0 and report.read_text() == "", second
        result = {
            "orphan_exit": first[0],
            "clean_exit": second[0],
            "all_orphan_ids_exported": len(rows),
            "business_rows_and_manual_fields_preserved": True,
        }
        Path(os.environ["RESULT_PATH"]).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        await database.engine.dispose()


asyncio.run(main())
