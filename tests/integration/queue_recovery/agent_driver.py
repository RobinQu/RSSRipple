"""Run Agent ownership rollback assertions on a dedicated PostgreSQL database."""

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
from app.models.downloader import DownloaderInstance  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from tests.integration.organize.test_agent_request_failures import (  # noqa: E402
    test_expired_incremental_run_preserves_publications_for_replacement,
)
from tests.unit.test_agent_service import (  # noqa: E402
    test_queue_loss_during_suggestion_does_not_persist_choice,
)


async def main():
    results = []
    try:
        async with database.engine.begin() as conn:
            assert not await conn.run_sync(lambda sync: inspect(sync).has_table("agents"))
            await conn.run_sync(database.Base.metadata.create_all)
        tables = ", ".join('"' + name + '"' for name in database.Base.metadata.tables)
        cases = [("choice", mode, phase) for mode in (False, True)
                 for phase in ("suggestion", "flushed_choice")]
        cases += [("cursor", None, phase) for phase in ("result", "exception")]
        for kind, mode, phase in cases:
            async with database.engine.begin() as conn:
                await conn.execute(text("TRUNCATE TABLE " + tables + " CASCADE"))
            async with database.async_session_factory() as db:
                with pytest.MonkeyPatch.context() as patcher:
                    if kind == "cursor":
                        await test_expired_incremental_run_preserves_publications_for_replacement(
                            db, database.async_session_factory, patcher, phase,
                        )
                    else:
                        channel = Channel(name="Synthetic", type="rss_feed", url="https://example.invalid", field_mapping={})
                        downloader = DownloaderInstance(name="Synthetic", type="mock", url="mock://synthetic", download_dir="/tmp/synthetic")
                        collection = WorkCollection(title_cn="Synthetic")
                        db.add_all([channel, downloader, collection])
                        await db.flush()
                        series = TVSeries(title_cn="Synthetic", collection_id=collection.id, season_number=1)
                        db.add(series)
                        await db.flush()
                        await test_queue_loss_during_suggestion_does_not_persist_choice(
                            db, channel, downloader, series, patcher, mode, phase,
                        )
            results.append({"case": kind, "autocommit": mode, "phase": phase, "passed": True})
        Path(os.environ["QUEUE_RECOVERY_RESULT"]).write_text(json.dumps({
            "backend": "postgresql", "cases": results,
            "data": "synthetic resources; real database transactions; deterministic ownership loss",
        }, indent=2) + "\n")
    finally:
        await database.engine.dispose()


asyncio.run(main())
