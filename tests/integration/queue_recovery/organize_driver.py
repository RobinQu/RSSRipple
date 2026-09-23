"""PostgreSQL and real filesystem coverage for organize ownership boundaries."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

database_url = os.environ["QUEUE_RECOVERY_DATABASE_URL"]
assert urlsplit(database_url).path.startswith("/queue_recovery_")
os.environ["DATABASE_URL"] = database_url

import pytest  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

from app import database  # noqa: E402
from app.config import settings  # noqa: E402
from tests.unit.test_queue_organize_ownership import (  # noqa: E402
    test_expired_queue_owner_cannot_move_files,
    test_started_move_retains_plan_lock_through_cleanup_after_queue_loss,
)


async def main():
    results = []
    try:
        async with database.engine.begin() as conn:
            assert not await conn.run_sync(lambda sync: inspect(sync).has_table("organize_plans"))
            await conn.run_sync(database.Base.metadata.create_all)
        tables = ", ".join('"' + name + '"' for name in database.Base.metadata.tables)
        for phase in ("before_lock", "after_validation", "started_cleanup"):
            async with database.engine.begin() as conn:
                await conn.execute(text("TRUNCATE TABLE " + tables + " CASCADE"))
            with tempfile.TemporaryDirectory(prefix="rssripple-organize-") as directory:
                root = Path(directory)
                async with database.async_session_factory() as db:
                    with pytest.MonkeyPatch.context() as patcher:
                        patcher.setattr(settings, "organize_lock_dir", str(root / "locks"))
                        if phase == "started_cleanup":
                            await test_started_move_retains_plan_lock_through_cleanup_after_queue_loss(
                                db, database.engine, root, patcher,
                            )
                        else:
                            await test_expired_queue_owner_cannot_move_files(db, root, patcher, phase)
            results.append({"phase": phase, "passed": True})
        Path(os.environ["QUEUE_RECOVERY_RESULT"]).write_text(json.dumps({
            "backend": "postgresql", "cases": results,
            "data": "synthetic media bytes; real filesystem locks and SQL; injected queue ownership loss",
        }, indent=2) + "\n")
    finally:
        await database.engine.dispose()


asyncio.run(main())
