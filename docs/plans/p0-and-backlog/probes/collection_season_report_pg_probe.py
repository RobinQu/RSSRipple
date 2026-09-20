"""Actual read-only CLI must report old PostgreSQL conflicts without startup."""

import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host in {"127.0.0.1", "localhost"}
assert (url.username, url.password, url.database) == ("organize_test",) * 3
from sqlalchemy import select, text  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.models.series import TVSeries  # noqa: E402


async def main():
    try:
        async with database.engine.begin() as conn:
            await conn.execute(text("DROP INDEX uq_tv_series_collection_season"))
        async with database.async_session_factory() as db:
            original = await db.scalar(select(TVSeries).order_by(TVSeries.id).limit(1))
            assert original is not None
            duplicate = TVSeries(
                title_cn="Synthetic protected duplicate",
                collection_id=original.collection_id,
                season_number=original.season_number,
                manually_edited_fields=["title_cn"],
            )
            db.add(duplicate)
            await db.commit()
            expected = {original.id, duplicate.id}
            before = {(r.id, r.title_cn, r.collection_id, r.season_number) for r in await db.scalars(select(TVSeries))}
        report = Path(os.environ["COLLECTION_REPORT_PATH"])
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "scripts.verify_season_split",
            "--collection-conflicts-jsonl",
            str(report),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        assert process.returncode == 1, (stdout.decode(), stderr.decode())
        rows = [json.loads(line) for line in report.read_text().splitlines()]
        assert {row["id"] for row in rows} == expected, rows
        assert any(row["manually_edited_fields"] == ["title_cn"] for row in rows)
        async with database.async_session_factory() as db:
            after = {(r.id, r.title_cn, r.collection_id, r.season_number) for r in await db.scalars(select(TVSeries))}
            assert before == after
        print(
            json.dumps(
                {
                    "fixture": "synthetic conflict; actual PostgreSQL and CLI subprocess",
                    "exit_code": process.returncode,
                    "members_reported": len(rows),
                    "full_ids_and_manual_protection_preserved": True,
                    "database_rows_unchanged": True,
                    "startup_not_required": True,
                },
                indent=2,
            )
        )
    finally:
        await database.engine.dispose()


asyncio.run(main())
