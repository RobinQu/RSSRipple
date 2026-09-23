"""Run the same metadata concurrency assertions against a fresh PostgreSQL DB."""

import asyncio
import json
import os
import subprocess
from pathlib import Path

PROJECT = "rssripple-v14-metadata-og"
[container] = json.loads(subprocess.check_output(["docker", "inspect", f"{PROJECT}-postgres-1"]))
assert container["Config"]["Labels"]["com.docker.compose.project"] == PROJECT
address = container["NetworkSettings"]["Networks"][PROJECT + "_isolated"]["IPAddress"]
os.environ["DATABASE_URL"] = f"postgresql+asyncpg://probe:probe@{address}:5432/probe"

import pytest  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

from app import database  # noqa: E402
from tests.unit.test_queue_metadata_ownership import (  # noqa: E402
    test_background_refresh_respects_concurrent_manual_edit,
    test_refresh_rejects_candidate_for_changed_work_scope,
    test_specials_fallback_preserves_concurrent_changes,
)


async def main():
    results = []
    try:
        async with database.engine.begin() as conn:
            assert not await conn.run_sync(lambda sync: inspect(sync).has_table("movies"))
            await conn.run_sync(database.Base.metadata.create_all)
        cases = [
            ("manual_title", test_background_refresh_respects_concurrent_manual_edit, value)
            for value in (False, True)
        ] + [
            ("specials", test_specials_fallback_preserves_concurrent_changes, value)
            for value in ("manual_date", "delete", "season", "collection")
        ] + [
            ("candidate_scope", test_refresh_rejects_candidate_for_changed_work_scope, value)
            for value in ("season", "collection", "delete")
        ]
        tables = ", ".join('"' + name + '"' for name in database.Base.metadata.tables)
        for label, test, value in cases:
            # Every table was created above in this dedicated, initially empty
            # database. Reset only our fixture before the next assertion set.
            async with database.engine.begin() as conn:
                await conn.execute(text("TRUNCATE TABLE " + tables + " CASCADE"))
            async with database.async_session_factory() as db:
                with pytest.MonkeyPatch.context() as patcher:
                    await test(db, patcher, value)
            results.append({"case": label, "variant": value, "passed": True})
        Path(os.environ["PROBE_RESULT_PATH"]).write_text(json.dumps({
            "backend": "postgresql", "cases": results,
            "data": "synthetic works/candidates, real independent database sessions",
            "network": "poster/search replaced by deterministic barriers",
        }, indent=2) + "\n")
    finally:
        await database.engine.dispose()


asyncio.run(main())
