"""Recorded multi-work inspection against an explicitly disposable PostgreSQL."""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import uuid
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

port = int(sys.argv[1])
os.environ["DATABASE_URL"] = f"postgresql+asyncpg://organize_test:organize_test@127.0.0.1:{port}/organize_test"

import pytest  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402

from app import database  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from app.models.file_resource import FileResource  # noqa: E402
from app.services import magnet_resolve as mr  # noqa: E402
from app.services import torrent_inspect as ti  # noqa: E402
from tests.unit.test_magnet_resolve import _make_magnet_resource  # noqa: E402

TORRENT = Path(
    "tests/fixtures/metadata_corpus_v1/torrents/912c2bd9bd70a9556cfaa974cd29d0f1748c05e26dbf6ae87453418a7f801ef1.torrent"
)
TABLES = ("resource_file_assignments", "resource_work_links", "movies", "tv_series", "work_collections")


async def main():
    results = []
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    try:
        for replace, llm_success in product((False, True), repeat=2):
            async with database.engine.begin() as conn:
                names = ",".join('"' + name + '"' for name in database.Base.metadata.tables)
                await conn.execute(text("TRUNCATE TABLE " + names + " CASCADE"))
            attempt, replacement = str(uuid.uuid4()), str(uuid.uuid4())
            async with database.async_session_factory() as db:
                channel = Channel(
                    name="Synthetic multiwork", type="rss_feed", url="https://example.invalid", field_mapping={}
                )
                db.add(channel)
                await db.flush()
                resource = await _make_magnet_resource(
                    db, channel.id, title_raw="Synthetic Cowboy Bebop pack", magnet_resolve_attempt_id=attempt
                )
                resource_id = resource.id
            calls = []

            async def metadata(title, source):
                calls.append(title)
                movie = "film" in title.lower()
                return SimpleNamespace(
                    found=True,
                    ambiguous=False,
                    content_type="movie" if movie else "tv",
                    matched_entity={
                        "title_en": "Synthetic Cowboy Film" if movie else "Synthetic Cowboy Season 1",
                        "external_source": "tmdb",
                        "external_id": "90000001" if movie else "90000002",
                        "content_type": "movie" if movie else "tv",
                        "season_number": 1,
                        "number_of_episodes": 26,
                    },
                )

            async def resolve(uri, dest, timeout, **kwargs):
                Path(dest).write_bytes(TORRENT.read_bytes())

            actual_inspect = ti.maybe_inspect_torrent
            report = ti.analyze_torrent_files(ti.parse_torrent_files(str(TORRENT)))
            listing_reply = (
                {
                    "works": [
                        {
                            "title": cluster.title,
                            "content_type": "movie" if "film" in cluster.title.lower() else "tv",
                            "files": [{"path": path} for path in cluster.files],
                        }
                        for cluster in report.clusters
                    ]
                }
                if llm_success
                else None
            )

            async def inspect(db, resource, channel):
                if replace:
                    async with database.async_session_factory() as newer:
                        saved = await newer.get(FileResource, resource_id)
                        saved.magnet_resolve_attempt_id = replacement
                        saved.magnet_resolve_status = "pending"
                        await newer.commit()
                return await actual_inspect(db, resource, channel)

            with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
                patch.setattr(mr.settings, "torrent_cache_dir", directory)
                patch.setattr(mr, "resolve_magnet_to_cache", resolve)
                patch.setattr(ti, "maybe_inspect_torrent", inspect)
                patch.setattr(
                    "app.services.batch_content_analysis.runtime_config",
                    SimpleNamespace(llm_api_key="synthetic-probe-key"),
                )
                patch.setattr(
                    "app.services.metadata_agent.get_agent", lambda: SimpleNamespace(process_title_only=metadata)
                )
                patch.setattr(
                    "app.services.batch_content_analysis.analyze_listing", AsyncMock(return_value=listing_reply)
                )
                await asyncio.wait_for(mr._attempt_loop(resource_id, attempt), timeout=40)
            async with database.async_session_factory() as observer:
                counts = {
                    name: (
                        await observer.execute(select(func.count()).select_from(database.Base.metadata.tables[name]))
                    ).scalar_one()
                    for name in TABLES
                }
                saved = await observer.get(FileResource, resource_id)
                assert calls, "actual multi-work metadata path was not reached"
                if replace:
                    assert saved.magnet_resolve_attempt_id == replacement
                    assert saved.magnet_resolve_status == "pending"
                    assert all(value == 0 for value in counts.values()), counts
                else:
                    assert saved.magnet_resolve_status == "done"
                    assert counts["resource_file_assignments"] == 27, counts
                    assert counts["movies"] >= 1 and counts["tv_series"] >= 1, counts
                    assert counts["resource_work_links"] >= 2 and counts["work_collections"] >= 1, counts
                    if llm_success:
                        assignments = database.Base.metadata.tables["resource_file_assignments"]
                        assert (
                            await observer.execute(
                                select(func.count())
                                .select_from(assignments)
                                .where(assignments.c.source == "llm", assignments.c.movie_id.is_not(None))
                            )
                        ).scalar_one() == 1
                results.append(
                    {"replace_attempt": replace, "llm_success": llm_success, "counts": counts, "metadata_calls": calls}
                )
        print(json.dumps({"sha256": hashlib.sha256(TORRENT.read_bytes()).hexdigest(), "cases": results}, indent=2))
    finally:
        await database.engine.dispose()


asyncio.run(main())
