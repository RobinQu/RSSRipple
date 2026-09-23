"""Validate internal commit ownership boundaries on disposable PostgreSQL."""

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

database_url = os.environ["QUEUE_RECOVERY_DATABASE_URL"]
assert urlsplit(database_url).path.startswith("/queue_recovery_")
os.environ["DATABASE_URL"] = database_url

import pytest  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

from app import database  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from tests.unit.test_fetch_service import test_reconcile_loss_preserves_episode_and_publications  # noqa: E402
from tests.unit.test_notify_service import seed  # noqa: E402
from tests.unit.test_queue_notification_ownership import (  # noqa: E402
    test_resource_regeneration_loss_rolls_back_snapshot_and_tokens,
)


async def main():
    results = []
    try:
        async with database.engine.begin() as conn:
            assert not await conn.run_sync(lambda sync: inspect(sync).has_table("file_resources"))
            await conn.run_sync(database.Base.metadata.create_all)
        tables = ", ".join('"' + name + '"' for name in database.Base.metadata.tables)
        cases = [("notification", phase) for phase in ("snapshot", "invalidated")]
        cases += [("reconcile", phase) for phase in ("entry", "published")]
        for kind, phase in cases:
            async with database.engine.begin() as conn:
                await conn.execute(text("TRUNCATE TABLE " + tables + " CASCADE"))
            async with database.async_session_factory() as db:
                with pytest.MonkeyPatch.context() as patcher:
                    if kind == "notification":
                        fixture = await seed.__wrapped__(db)
                        await test_resource_regeneration_loss_rolls_back_snapshot_and_tokens(db, fixture, patcher, phase)
                    else:
                        channel = Channel(name="Synthetic reconciliation", type="rss_feed",
                                          url="https://example.invalid", field_mapping={})
                        db.add(channel)
                        await db.flush()
                        await test_reconcile_loss_preserves_episode_and_publications(db, channel, patcher, phase)
            results.append({"case": kind, "phase": phase, "passed": True})
        Path(os.environ["QUEUE_RECOVERY_RESULT"]).write_text(json.dumps({
            "backend": "postgresql", "cases": results,
            "data": "synthetic fixtures; real SQL transactions; deterministic ownership loss",
        }, indent=2) + "\n")
    finally:
        await database.engine.dispose()


asyncio.run(main())
