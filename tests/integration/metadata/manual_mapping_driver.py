"""Disposable PostgreSQL: manual mapping arrives during automatic lookup."""
import asyncio
import gzip
import json
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

database_url = os.environ["DATABASE_URL"]
if database_url.startswith("sqlite+aioturso:"):
    probe_path = Path(urlsplit(database_url).path).resolve()
    assert probe_path.is_relative_to(Path("/tmp")) and probe_path.name.startswith("manual_mapping_")
else:
    assert urlsplit(database_url).path.startswith("/manual_mapping_")

from app import database  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping  # noqa: E402
from app.models.file_resource import FileResource  # noqa: E402
from app.models.movie import Movie  # noqa: E402
from app.services.metadata_agent import UnifiedMetadataAgent  # noqa: E402
from app.services.metadata_service import extract_search_title  # noqa: E402
from app.services.text_normalizer import normalize_title  # noqa: E402


async def main():
    editor_task = None
    try:
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)
            if database_url.startswith("sqlite+aioturso:"):
                from sqlalchemy import text
                await conn.execute(text("PRAGMA journal_mode='mvcc'"))
        async with database.async_session_factory() as db:
            channel = Channel(name="Synthetic concurrent mapping", url=f"https://example.invalid/{str(uuid.uuid4())}", field_mapping={},
                              metadata_source="tmdb")
            manual = Movie(title_en="Synthetic manual concurrent target", is_anime=False)
            automatic = Movie(title_en="Synthetic old automatic target", is_anime=False)
            db.add_all([channel, manual, automatic])
            await db.flush()
            raw_title = "Synthetic concurrent release 2024"
            if os.environ.get("RECORDED_TITLE") == "1":
                corpus = Path(__file__).resolve().parents[3] / "tests/fixtures/metadata_corpus_v1/candidates.json.gz"
                with gzip.open(corpus, "rt") as stream:
                    case = next(c for c in json.load(stream)["cases"]
                                if c["id"] == "00e48de8-b5fd-4e99-8467-d384bd4a3183")
                raw_title = case["input"]["title_raw"]
                print({"recorded_case": case["id"]}, flush=True)
            resource = FileResource(channel_id=channel.id, guid=str(uuid.uuid4()),
                                    title_raw=raw_title, torrent_url="")
            db.add(resource)
            await db.commit()
            rid, cid, mid = resource.id, channel.id, manual.id
            survivor_id = automatic.id
            key = normalize_title(extract_search_title(resource))
            if os.environ.get("PREEXISTING_MAPPING") == "1":
                db.add(ChannelRawTitleMapping(channel_id=cid, raw_title=resource.title_raw,
                           search_title_key=key, movie_id=mid, content_type="movie"))
                await db.commit()
            agent = UnifiedMetadataAgent()
            agent._ensure_genre = AsyncMock()

            async def edit_mapping():
                if os.environ.get("MERGE_API") == "1":
                    from fastapi import FastAPI
                    from httpx import ASGITransport, AsyncClient

                    from app.api.v1.works import router

                    async def merge_session():
                        async with database.async_session_factory() as session:
                            from sqlalchemy import text
                            if session.bind.dialect.name == "postgresql":
                                await session.execute(text("SET LOCAL application_name = 'm1-editor-probe'"))
                            yield session

                    api = FastAPI()
                    api.include_router(router, prefix="/api/v1")
                    api.dependency_overrides[database.get_db] = merge_session
                    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
                        response = await asyncio.wait_for(client.post("/api/v1/works/merge", json={
                            "survivor_type": "movie", "survivor_id": survivor_id,
                            "duplicate_ids": [mid], "confirm": True,
                        }), 5)
                    assert response.status_code == 200, response.text
                    assert response.json()["data"]["mappings_updated"] == 1
                    print({"merge_http_status": response.status_code}, flush=True)
                    return
                if os.environ.get("EDITOR_API") == "1":
                    from fastapi import FastAPI
                    from httpx import ASGITransport, AsyncClient

                    from app.api.v1.resources import router

                    async def editor_session():
                        async with database.async_session_factory() as session:
                            from sqlalchemy import text
                            if session.bind.dialect.name == "postgresql":
                                await session.execute(text("SET LOCAL application_name = 'm1-editor-probe'"))
                            yield session

                    api = FastAPI()
                    api.include_router(router, prefix="/api/v1")
                    api.dependency_overrides[database.get_db] = editor_session
                    with patch("app.api.v1.resources.task_queue_module.task_queue.enqueue", AsyncMock()):
                        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
                            response = await asyncio.wait_for(client.put(
                                f"/api/v1/resources/{rid}/associations",
                                json=({"is_batch": True, "works": []}
                                      if os.environ.get("EDITOR_SHAPE") == "1" else
                                      {"is_batch": False,
                                       "works": [{"work_type": "movie", "work_id": mid}],
                                       "fields": {"search_title": "User-selected title"}}),
                            ), 5)
                    assert response.status_code == 200, response.text
                    print({"editor_http_status": response.status_code}, flush=True)
                    return
                async with database.async_session_factory() as editor:
                    edited = await editor.get(FileResource, rid)
                    if os.environ.get("MAPPING_ONLY") != "1":
                        edited.movie_id = mid
                        edited.search_title = "User-selected title"
                    editor.add(ChannelRawTitleMapping(channel_id=cid, raw_title=edited.title_raw,
                               search_title_key=key, movie_id=mid, content_type="movie"))
                    await asyncio.wait_for(editor.commit(), 5)
            async def lookup(*args, **kwargs):
                if os.environ.get("LATE_MAPPING") != "1":
                    await edit_mapping()
                candidate_title = manual.title_en if os.environ.get("MERGE_API") == "1" else automatic.title_en
                return ({"found": True, "content_type": "movie", "clean_title": candidate_title,
                         "matched_entity": {"content_type": "movie", "title_en": candidate_title,
                                            "is_anime": False}},
                        {"method": "synthetic", "data_sources_used": [], "source_errors": {}, "error": None})

            agent._run_react = lookup
            shortcut = os.environ.get("SHORTCUT")
            if shortcut:
                from app.services.metadata_resource_meta import ResourceMetadata

                async def cached_lookup(*args, **kwargs):
                    await edit_mapping()
                    return ResourceMetadata(found=True, content_type="movie", clean_title=automatic.title_en,
                        matched_entity={"content_type": "movie", "title_en": automatic.title_en,
                                        "is_anime": False})

                async def known_lookup(*args, **kwargs):
                    await edit_mapping()
                    return ("movie", automatic.id)

                if shortcut == "cache":
                    agent._get_cache = cached_lookup
                else:
                    agent._get_cache = AsyncMock(return_value=None)
                    agent._find_known_work = known_lookup
            if os.environ.get("LATE_MAPPING") == "1":
                original_apply = agent._apply_to_resource

                async def apply_after_edit(*args, **kwargs):
                    nonlocal editor_task
                    if os.environ.get("REVERSE_API") == "1":
                        from sqlalchemy import text
                        editor_task = asyncio.create_task(edit_mapping())
                        async with database.engine.connect() as observer:
                            for _ in range(100):
                                blocked = await observer.scalar(text(
                                    "SELECT count(*) FROM pg_stat_activity "
                                    "WHERE application_name = 'm1-editor-probe' "
                                    "AND cardinality(pg_blocking_pids(pid)) > 0"
                                ))
                                if blocked:
                                    print({"editor_blocked_by_transaction": True}, flush=True)
                                    break
                                if editor_task.done():
                                    await editor_task
                                    if os.environ.get("MERGE_API") == "1":
                                        print({"merge_committed_before_candidate_write": True}, flush=True)
                                        break
                                    raise AssertionError("Editor committed before automatic lock released")
                                await asyncio.sleep(0.02)
                            else:
                                raise AssertionError("Editor database lock wait was not observed")
                    else:
                        await edit_mapping()
                    if os.environ.get("INJECT_UNIQUE") == "1":
                        from sqlalchemy import insert
                        with db.no_autoflush:
                            await db.execute(insert(Movie).values(id=survivor_id, title_en="Duplicate synthetic key"))
                    return await original_apply(*args, **kwargs)

                agent._apply_to_resource = apply_after_edit
            from sqlalchemy.exc import IntegrityError
            try:
                result = await asyncio.wait_for(agent.process(resource, channel, db, force_refresh=not bool(shortcut)), 15)
            except IntegrityError as exc:
                assert os.environ.get("INJECT_UNIQUE") == "1"
                assert getattr(exc.orig, "sqlstate", None) == "23505"
                print({"unrelated_unique_error_propagated": True}, flush=True)
                result = None
            else:
                assert os.environ.get("INJECT_UNIQUE") != "1", "Unique constraint failure was swallowed"
            await db.commit()
            if editor_task is not None:
                await asyncio.wait_for(editor_task, 5)
        async with database.async_session_factory() as observer:
            saved = await observer.get(FileResource, rid)
            print({"resource": rid, "expected_movie": mid, "actual_movie": saved.movie_id,
                   "search_title": saved.search_title}, flush=True)
            if os.environ.get("EDITOR_SHAPE") == "1":
                assert saved.is_batch and saved.batch_scope == "franchise"
                assert saved.movie_id is None and saved.series_id is None, "Old lookup overwrote batch shape"
            elif os.environ.get("MERGE_API") == "1":
                from sqlalchemy import select
                mapping = (await observer.execute(select(ChannelRawTitleMapping).where(
                    ChannelRawTitleMapping.channel_id == cid
                ))).scalar_one()
                assert mapping.movie_id == survivor_id
                assert await observer.get(Movie, mid) is None
                if os.environ.get("REVERSE_API") == "1":
                    assert saved.movie_id in (None, survivor_id)
                    if saved.movie_id is None:
                        assert result is None
                else:
                    assert result is None, "Old lookup must be discarded after mapping target merge"
                    assert saved.movie_id is None
                retry_channel = await observer.get(Channel, cid)
                retried = await agent.process(saved, retry_channel, observer)
                await observer.commit()
                await observer.refresh(saved)
                assert retried.found
                assert saved.movie_id == survivor_id
                print({"retry_linked_to_survivor": True}, flush=True)
            elif os.environ.get("MAPPING_ONLY") == "1":
                assert saved.movie_id in (None, mid), "Old lookup ignored the committed mapping"
            else:
                assert saved.movie_id == mid, "Old lookup overwrote the committed manual choice"
                assert saved.search_title == "User-selected title"
    finally:
        if editor_task is not None and not editor_task.done():
            editor_task.cancel()
            await asyncio.gather(editor_task, return_exceptions=True)
        await database.engine.dispose()


asyncio.run(main())
