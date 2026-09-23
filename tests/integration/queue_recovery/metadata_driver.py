"""Run the same metadata concurrency assertions against a fresh PostgreSQL DB."""

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
from tests.unit.test_queue_metadata_ownership import (  # noqa: E402
    test_background_refresh_respects_concurrent_manual_edit,
    test_refresh_rejects_candidate_for_changed_work_scope,
    test_resource_metadata_commit_boundaries,
    test_resource_metadata_loss_rolls_back_flushed_fields_and_publication,
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
        cases = [(label, test, (value,)) for label, test, value in cases]
        cases += [
            ("resource_commit", test_resource_metadata_commit_boundaries, (phase, kind))
            for phase in ("publication", "poster") for kind in ("movie", "series")
        ] + [
            ("resource_loss", test_resource_metadata_loss_rolls_back_flushed_fields_and_publication, (enabled, raises))
            for enabled in (False, True) for raises in (False, True)
        ]
        tables = ", ".join('"' + name + '"' for name in database.Base.metadata.tables)
        for label, test, value in cases:
            # Every table was created above in this dedicated, initially empty
            # database. Reset only our fixture before the next assertion set.
            async with database.engine.begin() as conn:
                await conn.execute(text("TRUNCATE TABLE " + tables + " CASCADE"))
            async with database.async_session_factory() as db:
                with pytest.MonkeyPatch.context() as patcher:
                    await test(db, patcher, *value)
            results.append({"case": label, "variant": value, "passed": True})
        Path(os.environ["QUEUE_RECOVERY_RESULT"]).write_text(json.dumps({
            "backend": "postgresql", "cases": results,
            "data": "synthetic works/candidates, real independent database sessions",
            "network": "poster/search replaced by deterministic barriers",
        }, indent=2) + "\n")
    finally:
        await database.engine.dispose()


asyncio.run(main())
