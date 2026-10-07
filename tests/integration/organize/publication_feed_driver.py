"""Actual feed producer with captured title/torrent and an explicit synthetic RSS shell."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
from xml.sax.saxutils import escape

import feedparser
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import selectinload

import app.database as database
import app.models  # noqa: F401
import app.services.fetch_service as fetch
from app.job_handlers import _handle_run_agent
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.resource_publication import ChannelPublicationCounter, ResourcePublication
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.metadata_concurrency import MetadataConcurrency
from app.services.torrent_inspect import parse_torrent_payload
from tests.metadata_corpus.dataset import ROOT, asset, digest, load_corpus, read_json

probe_url = make_url(os.environ["DATABASE_URL"])
assert probe_url.drivername == "sqlite+aioturso"
probe_path = Path(probe_url.database).resolve()
assert probe_path.name == "publication-feed.db"
assert probe_path.is_relative_to(Path(tempfile.gettempdir()).resolve())
assert probe_url.query["isolation_level"] in {"DEFERRED", "CONCURRENT"}

CASE = "f79ef2eb-02d5-42d3-80dc-70dd3c1d733b"


async def main():
    manifest, corpus, reviews = load_corpus(ROOT)
    case = next(c for c in corpus["cases"] if c["id"] == CASE)
    review = reviews[CASE]
    assert review["status"] == "confirmed" and not case["input"]["raw_rss_available"]
    torrent = asset(ROOT, case["evidence"]["torrent"])
    raw = torrent.read_bytes()
    assert digest(raw) == torrent.stem
    listing_path = asset(ROOT, manifest["file_list_file"])
    assert digest(listing_path.read_bytes()) == manifest["file_list_sha256"]
    listing = read_json(listing_path)[case["evidence"]["torrent"]]
    assert parse_torrent_payload(raw) == listing
    [assignment] = review["expected"]["assignments"]
    assert assignment["file_path"] == listing[0]["name"]
    title = case["input"]["title_raw"]
    rss = f'<rss version="2.0"><channel><title>Synthetic RSS shell</title><item><guid>captured-title-1</guid><title>{escape(title)}</title><enclosure url="https://example.invalid/captured.torrent" type="application/x-bittorrent" length="{len(raw)}"/></item></channel></rss>'
    parsed_feed = feedparser.parse(rss)
    assert not parsed_feed.bozo and len(parsed_feed.entries) == 1
    async with database.engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode='mvcc'"))
        await conn.run_sync(database.Base.metadata.create_all)
    async with database.async_session_factory() as db:
        channel = Channel(
            name="Captured title publication",
            url="https://example.invalid/feed",
            field_mapping={},
            metadata_agent_enabled=False,
        )
        downloader = DownloaderInstance(name="Synthetic", type="mock", url="mock://synthetic", download_dir="/tmp")
        collection = WorkCollection(title_cn="猫与龙")
        db.add_all([channel, downloader, collection])
        await db.flush()
        series = TVSeries(
            title_cn="猫与龙",
            original_title="猫と竜",
            collection_id=collection.id,
            season_number=1,
            number_of_episodes=12,
            content_type="tv",
            is_anime=True,
        )
        agent = Agent(name="Feed consumer", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=True)
        db.add_all([series, agent])
        await db.commit()
        cid, aid, sid = channel.id, agent.id, series.id
    assert (await _handle_run_agent({"agent_id": aid}))["total_resources"] == 0
    observed_created = []

    async def reviewed_link(db, resource, channel):
        # Explicit reviewed metadata adapter, not a claim of cold metadata inference.
        async with database.async_session_factory() as observer:
            producer_connection = await db.connection()
            observer_connection = await observer.connection()
            # An in-memory StaticPool would let observer.close() roll back
            # the producer's physical transaction. This must be a real reader.
            assert (
                producer_connection.sync_connection.connection.driver_connection
                is not observer_connection.sync_connection.connection.driver_connection
            )
            assert await observer.get(FileResource, resource.id) is not None
            assert (
                await observer.scalar(
                    select(ResourcePublication.kind).where(ResourcePublication.resource_id == resource.id)
                )
                == "created"
            )
        observed_created.append(resource.id)
        resource.series_id = sid
        resource.season = assignment["season"]
        resource.episode = assignment["episode_start"]
        resource.episode_confidence = "reconciled"
        resource.content_type = "tv"

    failure = os.environ.get("FAIL_KIND", "")
    queue_recovery = os.environ.get("QUEUE_RECOVERY") == "1"
    lost_wakeup = AsyncMock(side_effect=RuntimeError("Synthetic queue outage") if queue_recovery else None)

    def reject_event(conn, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith("insert into resource_publications"):
            if any(row.get("kind") == failure for row in context.compiled_parameters):
                raise RuntimeError("Synthetic publication INSERT failure: " + failure)

    if failure:
        event.listen(database.engine.sync_engine, "before_cursor_execute", reject_event)
    with (
        patch.object(fetch, "_parse_feed_sync", return_value=parsed_feed),
        patch.object(fetch, "fetch_and_link_metadata", reviewed_link),
        patch("app.services.torrent_inspect.ensure_torrent_cached", AsyncMock()),
        patch("app.services.torrent_inspect.maybe_inspect_torrent", AsyncMock()),
        patch("app.services.task_queue.task_queue.enqueue", lost_wakeup),
    ):
        try:
            async with database.async_session_factory() as db:
                channel = await db.get(Channel, cid, options=[selectinload(Channel.agents)])
                try:
                    fetched = await fetch.fetch_channel_resources(channel, db)
                except RuntimeError:
                    assert failure == "created"
                    await db.rollback()
                    fetched = {"creation_rolled_back": True}
        finally:
            if failure:
                event.remove(database.engine.sync_engine, "before_cursor_execute", reject_event)
        async with database.async_session_factory() as db:
            resources = list(await db.scalars(select(FileResource)))
            publications = list(await db.scalars(select(ResourcePublication).order_by(ResourcePublication.sequence)))
            kinds = [p.kind for p in publications]
            if failure == "created":
                assert not resources and not publications
                assert await db.scalar(select(ChannelPublicationCounter.id)) is None
            else:
                assert len(resources) == 1 and resources[0].title_raw == title
                assert observed_created == [resources[0].id]
                if failure == "metadata":
                    assert kinds == ["created"] and resources[0].series_id is None
                else:
                    assert kinds == ["created", "metadata"] and resources[0].series_id == sid
        if failure == "metadata":
            before = await _handle_run_agent({"agent_id": aid})
            assert before["unrecognized"] == 1 and before["dispatched"] == 0
            await fetch._process_resource_metadata(resources[0].id, cid, MetadataConcurrency(1))
        if queue_recovery:
            from app.services.publication_dispatch import dispatch_pending_publications
            from app.services.task_queue import MemoryQueue

            lost_wakeup.assert_awaited_once()
            queue = MemoryQueue()
            queue.register("run_agent", _handle_run_agent)
            with patch("app.services.task_queue.task_queue", queue):
                await queue.start()
                try:
                    await dispatch_pending_publications()
                    async with asyncio.timeout(15):
                        while True:
                            status = await queue.status(f"agent:{aid}")
                            if status and status["status"] in {"done", "failed"}:
                                break
                            await asyncio.sleep(0.02)
                    assert status["status"] == "done", status
                    consumed = status["result"]
                    await dispatch_pending_publications()
                    assert (await queue.status(f"agent:{aid}"))["job_id"] == status["job_id"]
                finally:
                    await queue.stop()
        else:
            consumed = await _handle_run_agent({"agent_id": aid})
        assert consumed["dispatched"] == (0 if failure == "created" else 1), consumed
        assert not consumed["errors"]
        again = await _handle_run_agent({"agent_id": aid})
        assert again["total_resources"] == 0, again
    result = {
        "case": CASE,
        "torrent_sha256": torrent.stem,
        "rss": "synthetic shell; recorded raw RSS unavailable",
        "metadata": "explicit reviewed adapter",
        "failure": failure or None,
        "lost_wakeup_recovered_by_live_queue": queue_recovery,
        "fetched": fetched,
        "events_after_fetch": kinds,
        "consumed": consumed,
        "next_run": again,
    }
    Path(os.environ["PROBE_RESULT_PATH"]).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    await database.engine.dispose()


asyncio.run(main())
