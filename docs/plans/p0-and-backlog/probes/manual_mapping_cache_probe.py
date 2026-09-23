"""Compare real mapping/cache persistence using a recorded release title."""

import asyncio
import gzip
import json
import os
import tempfile
import uuid

scratch = tempfile.TemporaryDirectory(prefix="rssripple-mapping-")
os.environ["DATABASE_URL"] = f"sqlite+aioturso:///{scratch.name}/probe.db?isolation_level=DEFERRED"

from app import database  # noqa: E402
from app.models import Channel, ChannelRawTitleMapping, FileResource, Movie  # noqa: E402
from app.services.metadata_agent import UnifiedMetadataAgent  # noqa: E402
from app.services.metadata_resource_meta import ResourceMetadata  # noqa: E402
from app.services.metadata_service import extract_search_title, fetch_and_link_metadata  # noqa: E402
from app.services.text_normalizer import normalize_title  # noqa: E402


async def main():
    try:
        corpus = json.load(gzip.open("tests/fixtures/metadata_corpus_v1/candidates.json.gz", "rt"))
        case = next(
            c
            for c in corpus["cases"]
            if any(w.startswith("movie:") for w in c.get("candidate_expected", {}).get("works", []))
        )
        raw = case["input"]["title_raw"]
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)
        agent = UnifiedMetadataAgent()
        async with database.async_session_factory() as db:
            channel = Channel(
                name="Synthetic mapping conflict",
                type="rss_feed",
                url="https://example.invalid",
                field_mapping={},
                metadata_source="tmdb",
            )
            manual = Movie(title_en="Synthetic Manual Choice", is_anime=False)
            cached = Movie(title_en="Synthetic Cached Choice", is_anime=False)
            db.add_all([channel, manual, cached])
            await db.flush()
            resources = [
                FileResource(channel_id=channel.id, guid=str(uuid.uuid4()), title_raw=raw, torrent_url="")
                for _ in range(2)
            ]
            db.add_all(resources)
            await db.flush()
            db.add(
                ChannelRawTitleMapping(
                    channel_id=channel.id,
                    raw_title=raw,
                    search_title_key=normalize_title(extract_search_title(resources[0])),
                    movie_id=manual.id,
                    content_type="movie",
                )
            )
            await agent._set_cache(
                raw,
                "tmdb",
                ResourceMetadata(
                    clean_title=cached.title_en,
                    content_type="movie",
                    found=True,
                    matched_entity={"title_en": cached.title_en, "content_type": "movie", "is_anime": False},
                ),
                db,
            )
            await db.commit()
            await fetch_and_link_metadata(db, resources[0], channel)
            await agent.process(resources[1], channel, db)
            await db.commit()
            ids = [r.id for r in resources]
            expected, conflict = manual.id, cached.id
        async with database.async_session_factory() as observer:
            observed = [(await observer.get(FileResource, rid)).movie_id for rid in ids]
        report = {
            "case_id": case["id"],
            "recorded_title": raw,
            "mapping_path": observed[0],
            "agent_path": observed[1],
            "manual_work": expected,
            "cached_work": conflict,
            "data": "Recorded title; synthetic conflicting works, mapping and successful cache; real Turso persistence",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        assert observed[0] == expected, "control mapping path did not execute"
        assert observed[1] == expected, "default agent cache bypassed manual mapping"
    finally:
        await database.engine.dispose()
        scratch.cleanup()


asyncio.run(main())
