"""Run cache failure assertions in a dedicated empty PostgreSQL database."""
import asyncio
import json
import os
from itertools import product
from pathlib import Path
from urllib.parse import urlsplit

url = os.environ["WIKI_FAILURE_DATABASE_URL"]
assert urlsplit(url).path.startswith("/wiki_failure_")
os.environ["DATABASE_URL"] = url

import pytest  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

from app import database  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from tests.unit.test_wiki_failure_cache import (  # noqa: E402
    test_failed_page_lookup_preserves_retry_outcome,
    test_tmdb_failed_search_is_not_negative_cached,
    test_tmdb_partial_success_retains_candidate_without_caching,
    test_total_wiki_failure_does_not_poison_cache,
    test_web_fallback_preserves_primary_failure_or_success,
    test_wiki_partial_failure_keeps_grounded_success,
)


async def main():
    results = []
    try:
        async with database.engine.begin() as conn:
            assert not await conn.run_sync(lambda c: inspect(c).has_table("metadata_cache"))
            await conn.run_sync(database.Base.metadata.create_all)
        tables = ", ".join('"' + name + '"' for name in database.Base.metadata.tables)
        for partial_failure, web_negative in product((False, True), repeat=2):
            async with database.engine.begin() as conn:
                await conn.execute(text("TRUNCATE TABLE " + tables + " CASCADE"))
            async with database.async_session_factory() as db:
                channel = Channel(name="Synthetic failure channel", url="https://unused.invalid/feed", field_mapping={})
                db.add(channel)
                await db.commit()
                with pytest.MonkeyPatch.context() as patch:
                    await test_total_wiki_failure_does_not_poison_cache(
                        db, channel, patch, partial_failure, web_negative,
                    )
                results.append(f"real_database_failure_recovery_{partial_failure}_{web_negative}")
        for found in (False, True):
            with pytest.MonkeyPatch.context() as patch:
                await test_web_fallback_preserves_primary_failure_or_success(patch, found)
            results.append("web_fallback_" + str(found))
        for recovered in (False, True):
            with pytest.MonkeyPatch.context() as patch:
                await test_failed_page_lookup_preserves_retry_outcome(patch, recovered)
            results.append("page_retry_" + str(recovered))
        for languages in ({"zh-CN", "en-US"}, {"zh-CN"}):
            with pytest.MonkeyPatch.context() as patch:
                await test_tmdb_failed_search_is_not_negative_cached(patch, languages)
            results.append("tmdb_failed_" + str(len(languages)))
        with pytest.MonkeyPatch.context() as patch:
            await test_tmdb_partial_success_retains_candidate_without_caching(patch)
        results.append("tmdb_partial_success")
        with pytest.MonkeyPatch.context() as patch:
            await test_wiki_partial_failure_keeps_grounded_success(patch)
        results.append("wiki_partial_success")
        Path(os.environ["WIKI_FAILURE_RESULT"]).write_text(json.dumps({"cases": results}))
    finally:
        await database.engine.dispose()


asyncio.run(main())
