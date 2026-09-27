"""Dedicated PostgreSQL: reuse season evidence assertions with real commits."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

url = os.environ["SEASON_EVIDENCE_DATABASE_URL"]
assert urlsplit(url).path.startswith("/season_evidence_")
os.environ["DATABASE_URL"] = url

import pytest  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

from app import database  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from tests.unit.test_collection_member_season_evidence import (  # noqa: E402
    test_collection_size_does_not_supply_season,
    test_explicit_manual_target_supplies_its_season,
    test_recorded_title_preserves_unknown_season,
    test_season_identity_does_not_supply_season_number,
    test_series_level_unknown_season_cannot_use_outer_defaults,
)


async def main():
    results = []
    try:
        async with database.engine.begin() as conn:
            assert not await conn.run_sync(lambda c: inspect(c).has_table("tv_series"))
            await conn.run_sync(database.Base.metadata.create_all)
        table_names = ", ".join('"' + name + '"' for name in database.Base.metadata.tables)
        async def reset():
            async with database.engine.begin() as conn:
                await conn.execute(text("TRUNCATE TABLE " + table_names + " CASCADE"))
        cases = [([0], None, None, None), ([1], None, None, None), ([3], None, None, None),
                 ([1], None, 3, None), ([], None, None, None), ([1, 3], None, None, None),
                 ([3], 3, None, 3), ([1], None, 1, 1)]
        for route in ("title", "collection_bag"):
            for members, hint, count, expected in cases:
                await reset()
                async with database.async_session_factory() as db:
                    await test_collection_size_does_not_supply_season(db, route, members, hint, count, expected)
                results.append({"case": "evidence", "route": route, "members": members, "hint": hint, "count": count})
        for sibling in (False, True):
            await reset()
            async with database.async_session_factory() as db:
                await test_explicit_manual_target_supplies_its_season(db, sibling)
            results.append({"case": "manual_target", "sibling": sibling})
        for entry in ("repository", "agent", "cache", "pipeline"):
            for member in (None, 3):
                for hint in (None, 3):
                    await reset()
                    async with database.async_session_factory() as db:
                        channel = Channel(name="Synthetic recorded-title channel", url="https://example.invalid", field_mapping={})
                        db.add(channel)
                        await db.commit()
                        with pytest.MonkeyPatch.context() as patch:
                            await test_recorded_title_preserves_unknown_season(db, channel, patch, entry, member, hint)
                    results.append({"case": "recorded_resource", "entry": entry, "member": member, "hint": hint})
        for existing in (False, True):
            await reset()
            async with database.async_session_factory() as db:
                await test_series_level_unknown_season_cannot_use_outer_defaults(db, existing)
            results.append({"case": "outer_series_default", "existing": existing})
        for existing in (False, True):
            await reset()
            async with database.async_session_factory() as db:
                await test_season_identity_does_not_supply_season_number(db, existing)
            results.append({"case": "season_identity_no_ordinal", "existing": existing})
        Path(os.environ["SEASON_EVIDENCE_RESULT"]).write_text(json.dumps({"cases": results}))
    finally:
        await database.engine.dispose()


asyncio.run(main())
