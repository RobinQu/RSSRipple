"""Run the production fetch pipeline in a disposable database, then compare graphs."""

from __future__ import annotations

import socket
import time
import uuid
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import feedparser
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from .dataset import FIELDS, asset, load_corpus, resource_answer, validate_review
from .replay import Cassette


def database_socket_addresses(url: str) -> set[tuple]:
    """Resolve only the isolated PG endpoint before installing the socket guard.

    Docker service names and localhost are resolved to numeric socket addresses;
    permit those exact addresses/ports, not every socket to a database host.
    """
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        return set()
    if parsed.database != "metadata_corpus_test":
        raise ValueError("PostgreSQL database must be named metadata_corpus_test")
    if not parsed.host:
        raise ValueError("PostgreSQL corpus URL requires an explicit TCP host")
    port = parsed.port or 5432
    addresses = {row[4] for row in socket.getaddrinfo(parsed.host, port, type=socket.SOCK_STREAM)}
    return addresses | {(parsed.host, port)}


async def verify_dispatch(db, resource, required_fields, temporary):
    """Real filtering/dedup/submission; only the downloader itself is simulated."""
    from app.models.agent import Agent
    from app.models.download_task import DownloadTask
    from app.models.downloader import DownloaderInstance
    from app.services.agent_service import process_resources
    downloader = DownloaderInstance(name="corpus", type="mock", url="mock://corpus", download_dir=temporary)
    db.add(downloader)
    await db.flush()
    agent = Agent(name="corpus", channel_id=resource.channel_id, downloader_id=downloader.id,
                  scope_channel_wide=True, llm_enabled=False, conflict_resolution="auto", works=[])
    db.add(agent)
    await db.flush()
    first = await process_resources(agent, [resource], db, required_metadata_fields=required_fields)
    second = await process_resources(agent, [resource], db, required_metadata_fields=required_fields)
    tasks = (await db.execute(select(DownloadTask).where(DownloadTask.agent_id == agent.id))).scalars().all()
    return {"dispatched": first.dispatched, "repeat_dispatched": second.dispatched,
            "task_count": len(tasks), "error_tasks": sum(t.status == "error" for t in tasks)}


@asynccontextmanager
async def isolated_database(url: str):
    """Never create tables in a production database; PG gets a per-run schema."""
    import app.database as database
    import app.models  # noqa: F401
    from app.config import settings
    from app.database import Base, apply_db_pragmas, normalize_database_url
    from app.services import fts

    parsed = make_url(url)
    postgres = parsed.drivername.startswith("postgresql")
    if postgres and parsed.database != "metadata_corpus_test":
        raise ValueError("PostgreSQL database must be named metadata_corpus_test")
    schema = "corpus_" + uuid.uuid4().hex
    admin = None
    if postgres:
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        if not parsed.drivername.startswith("sqlite+aioturso"):
            raise ValueError("only PostgreSQL and temporary Turso databases are supported")
        path = Path(parsed.database).resolve()
        if path.exists():
            raise ValueError("Turso test database must be a new file")
        engine = create_async_engine(normalize_database_url(url))
        apply_db_pragmas(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sidecar = None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if not postgres:
                await conn.execute(text("PRAGMA journal_mode='mvcc'"))
        if not postgres:
            sidecar = create_async_engine(f"sqlite+aioturso:///{path}.fts?experimental_features=index_method")
        with (patch.object(database, "engine", engine),
              patch.object(database, "async_session_factory", factory),
              patch.object(settings, "database_url", url), patch.object(fts, "_FTS_ENGINE", sidecar)):
            if sidecar is not None:
                await fts.ensure_fts_tables()
            yield factory
    finally:
        if sidecar is not None:
            await sidecar.dispose()
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def database_graph(db) -> dict:
    from app.database import Base
    models = {m.local_table.name: m.class_ for m in Base.registry.mappers}
    graph = {}
    for table, columns in FIELDS.items():
        model = models[table]
        rows = (await db.execute(select(model))).scalars().unique().all()
        graph[table] = [{key: getattr(row, key) for key in columns.split() if hasattr(model, key)} for row in rows]
    return graph


def compare(expected, actual, path="") -> list[dict]:
    """Expected fields are explicit assertions; lists are complete, not subsets."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        differences = []
        for key, value in expected.items():
            differences.extend(compare(value, actual.get(key), f"{path}/{key}"))
        return differences
    if expected != actual:
        return [{"path": path, "expected": expected, "actual": actual}]
    return []


async def run_scenario(root: Path, scenario: dict, database_url: str, *, mode="replay") -> dict:
    from app.clients import mock_downloader
    from app.config import settings
    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries
    from app.services import fetch_service, metadata_search_agent, runtime_config
    from app.services.metadata_agent import reset_metadata_agent
    from app.services.metadata_wikipedia_client import _wikipedia_client
    from app.services.resource_confirmation import inspect_resource_confirmation
    from app.services.task_queue import task_queue

    _, corpus, reviews = load_corpus(root)
    by_id = {c["id"]: c for c in corpus["cases"]}
    selected = [by_id[key] for key in scenario["case_ids"]]
    if not selected:
        raise ValueError("scenario must contain at least one case")
    for case in selected:
        if mode not in {"record", "record-llm"} and reviews.get(case["id"], {}).get("status") != "confirmed":
            raise ValueError(f"scenario uses unreviewed case {case['id']}")
        if mode not in {"record", "record-llm"}:
            validate_review(reviews[case["id"]], case["id"])
    sources = {c["id"]: c for c in corpus["graph"]["channels"]}
    assets = {f"https://corpus.invalid/{c['id']}.torrent": asset(root, c["evidence"]["torrent"]).read_bytes()
              for c in selected if c["evidence"]["torrent"]}
    cassette = Cassette(asset(root, scenario["cassette"]), mode=mode,
                        source_hosts=scenario["source_hosts"], llm_host=scenario["llm_host"], local_assets=assets,
                        database_addresses=database_socket_addresses(database_url),
                        seed=asset(root, scenario["seed"]) if mode == "record-llm" else None)
    started = time.monotonic()
    differences = []
    rows = []
    # Temporary local caches, never the production data directory.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="metadata-corpus-assets-") as temporary, ExitStack() as stack:
        stack.enter_context(patch.object(mock_downloader, "_STATE", {}))
        stack.enter_context(patch.object(metadata_search_agent, "_cache", {}))
        stack.enter_context(patch.object(metadata_search_agent, "_TMDB_GENRE_MAP", None))
        # Also unwind on a missing cassette/DB error, so a failed corpus run
        # cannot leak a title index or cache into the next integration module.
        stack.callback(reset_metadata_agent)
        stack.callback(_wikipedia_client.cache_clear)
        stack.callback(metadata_search_agent._tmdb_image_base.cache_clear)
        stack.enter_context(patch.object(settings, "torrent_cache_dir", temporary))
        stack.enter_context(patch.object(settings, "poster_cache_dir", temporary))
        stack.enter_context(patch.object(task_queue, "enqueue", AsyncMock()))
        stack.enter_context(patch.object(fetch_service, "MAX_METADATA_CONCURRENCY", 1))
        stack.enter_context(patch.object(fetch_service, "_backfill_unmatched_resources", AsyncMock(return_value=0)))
        # Source availability is fixture configuration; no secret values are persisted.
        config = {"llm_model": scenario["model"], "llm_base_url": scenario["llm_base_url"],
                  "wikipedia_enabled": "true", "tmdb_enabled": "true", "web_fallback_enabled": "true"}
        if mode == "replay":
            config.update(llm_api_key="corpus", tmdb_api_key="corpus", bangumi_api_key="corpus")
        stack.enter_context(patch.dict(runtime_config._overrides, config))
        # Freeze application UTC clocks, without freezing event-loop deadlines.
        import sys

        from app.utils.time import utcnow
        frozen = datetime.fromisoformat(scenario.get("clock", "2026-09-05T00:00:00"))
        for name, module in list(sys.modules.items()):
            if name.startswith("app.") and getattr(module, "utcnow", None) is utcnow:
                stack.enter_context(patch.object(module, "utcnow", lambda: frozen))
        reset_metadata_agent()
        metadata_search_agent._tmdb_image_base.cache_clear()
        _wikipedia_client.cache_clear()
        async with isolated_database(database_url) as factory:
            with cassette:
                async with factory() as db:
                    channels = {}
                    for case in selected:
                        channel_id = case["channel_id"]
                        if channel_id not in channels:
                            fields = dict(sources[channel_id])
                            fields.update(url="https://corpus.invalid/feed", metadata_agent_enabled=True)
                            # Historical RSS fields are unavailable. The declared
                            # title/torrent mode tests the production normalizer,
                            # not reconstructed channel-regex captures.
                            fields["field_mapping"] = {"title_raw": {"source": "title"}}
                            channel = Channel(**fields, agents=[])
                            db.add(channel)
                            channels[channel_id] = channel
                    await db.commit()
                    for index, case in enumerate(selected):
                        if scenario.get("refresh_title_index_between_entries") and index:
                            from app.services.metadata_agent import get_agent
                            get_agent()._title_index_store._title_index_at = 0
                        channel = (await db.execute(select(Channel).where(Channel.id == case["channel_id"])
                                   .options(selectinload(Channel.agents)))).scalar_one()
                        raw = case["input"]["title_raw"]
                        url = f"https://corpus.invalid/{case['id']}.torrent"
                        entry = feedparser.FeedParserDict(
                            id=f"corpus-{index}", title=raw, link=url,
                            links=[{"rel": "enclosure", "href": url, "type": "application/x-bittorrent"}],
                        )
                        feed = SimpleNamespace(bozo=False, entries=[entry])
                        with patch.object(fetch_service, "_parse_feed_sync", return_value=feed):
                            await fetch_service.fetch_channel_resources(channel, db)
                        db.expire_all()
                        resource = (await db.execute(select(FileResource).where(FileResource.guid == f"corpus-{index}")
                            .options(selectinload(FileResource.series).selectinload(TVSeries.collection),
                                     selectinload(FileResource.movie).selectinload(Movie.collection),
                                     selectinload(FileResource.collection),
                                     selectinload(FileResource.work_links).selectinload(ResourceWorkLink.series)
                                         .selectinload(TVSeries.collection),
                                     selectinload(FileResource.work_links).selectinload(ResourceWorkLink.movie)
                                         .selectinload(Movie.collection)))).scalar_one()
                        graph = await database_graph(db)
                        row = next(r for r in graph["file_resources"] if r["id"] == resource.id)
                        actual = resource_answer(row, graph)
                        confirmation = inspect_resource_confirmation(resource, sources[case["channel_id"]]["required_metadata_fields"])
                        actual["confirmation"] = {"kinds": list(confirmation.kinds), "missing_fields": list(confirmation.missing_fields)}
                        expected = reviews.get(case["id"], {}).get("expected", case["candidate_expected"])
                        if "dispatch" in expected:
                            actual["dispatch"] = await verify_dispatch(
                                db, resource, sources[case["channel_id"]]["required_metadata_fields"], temporary,
                            )
                        differences.extend(compare(expected, actual, case["id"]))
                        rows.append({"case_id": case["id"], "actual": actual})
                cassette.assert_complete()
    return {"scenario": scenario["id"], "mode": mode, "model": scenario["model"], "passed": not differences,
            "differences": differences, "results": rows, "calls": dict(cassette.calls),
            "elapsed_seconds": round(time.monotonic() - started, 3)}
