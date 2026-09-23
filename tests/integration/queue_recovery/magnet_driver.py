"""Exercise magnet attempt isolation on a disposable PostgreSQL database."""

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

database_url = os.environ["QUEUE_RECOVERY_DATABASE_URL"]
assert urlsplit(database_url).path.startswith("/queue_recovery_")
os.environ["DATABASE_URL"] = database_url

import pytest  # noqa: E402
from sqlalchemy import MetaData, inspect, select, text  # noqa: E402

from app import database  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from tests.integration.queue_recovery.magnet_crash import verify_recovery  # noqa: E402
from tests.unit.test_job_handlers import test_magnet_sweep_propagates_ownership_loss  # noqa: E402
from tests.unit.test_magnet_resolve import (  # noqa: E402
    test_inspection_discards_changes_after_attempt_replaced,
    test_reclaimed_resource_can_launch_while_old_task_waits,
    test_stale_manual_retry_preserves_new_claim,
)


async def main():
    results = []
    try:
        async with database.engine.begin() as conn:
            assert not await conn.run_sync(lambda sync: inspect(sync).has_table("file_resources"))
            legacy = MetaData()
            for table in database.Base.metadata.sorted_tables:
                table.to_metadata(legacy)
            resources = legacy.tables["file_resources"]
            resources._columns.remove(resources.c.magnet_resolve_attempt_id)
            await conn.run_sync(legacy.create_all)
            channel_id = str(uuid.uuid4())
            await conn.execute(legacy.tables["channels"].insert().values(
                id=channel_id, name="Synthetic migration", type="rss_feed",
                url="https://example.invalid", field_mapping={},
            ))
            for index, status in enumerate((None, "running", "done")):
                await conn.execute(resources.insert().values(
                    id=str(uuid.uuid4()), channel_id=channel_id, guid=str(index),
                    title_raw="Synthetic", torrent_url="magnet:?xt=synthetic",
                    magnet_resolve_status=status,
                ))
        for _ in range(2):
            async with database.engine.begin() as conn:
                await database._apply_light_migrations(conn)
        current = database.Base.metadata.tables["file_resources"]
        async with database.engine.connect() as conn:
            rows = (await conn.execute(select(
                current.c.magnet_resolve_status, current.c.magnet_resolve_attempt_id,
            ).order_by(current.c.guid))).all()
            assert rows == [(None, None), ("running", None), ("done", None)]
        results.append({"case": "legacy_migration_twice", "passed": True})
        tables = ", ".join('"' + name + '"' for name in database.Base.metadata.tables)
        for case in ("manual_retry", "inspection", "reclaimed", "entry", "after_update", "enqueue", "crash"):
            async with database.engine.begin() as conn:
                await conn.execute(text("TRUNCATE TABLE " + tables + " CASCADE"))
            async with database.async_session_factory() as db:
                channel = Channel(name="Synthetic magnet race", type="rss_feed", url="https://example.invalid", field_mapping={})
                db.add(channel)
                await db.flush()
                with pytest.MonkeyPatch.context() as patcher, tempfile.TemporaryDirectory() as directory:
                    if case == "manual_retry":
                        await test_stale_manual_retry_preserves_new_claim(db, channel, patcher)
                    elif case == "inspection":
                        await test_inspection_discards_changes_after_attempt_replaced(db, channel, patcher, Path(directory))
                    elif case == "reclaimed":
                        await test_reclaimed_resource_can_launch_while_old_task_waits(db, channel, patcher)
                    elif case == "crash":
                        crash_result = await verify_recovery(db, channel, patcher, Path(directory))
                    else:
                        await db.rollback()
                        await test_magnet_sweep_propagates_ownership_loss(database.engine, patcher, case)
            results.append(crash_result if case == "crash" else {"case": case, "passed": True})
        Path(os.environ["QUEUE_RECOVERY_RESULT"]).write_text(json.dumps({
            "backend": "postgresql", "cases": results,
            "data": "synthetic resources; real concurrent database sessions; mocked libtorrent and inspection",
        }, indent=2) + "\n")
    finally:
        await database.engine.dispose()


asyncio.run(main())
