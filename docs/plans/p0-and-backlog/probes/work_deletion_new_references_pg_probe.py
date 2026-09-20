# ruff: noqa: E402
import asyncio
import os

from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
assert (url.username, url.password, url.database) == ("organize_test",) * 3
import json
import uuid
from pathlib import Path
from unittest.mock import patch

import app.services.work_deletion as deletion
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.database as database
import app.models  # noqa: F401
from app.api.v1.movies import delete_movie
from app.api.v1.series import delete_series
from app.models.agent import Agent
from app.models.agent_work import AgentWork
from app.models.channel import Channel
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection


async def case(kind, model, insert_first):
    async with database.async_session_factory() as db:
        channel = Channel(
            name="Synthetic FK race", url="https://example.invalid/" + str(uuid.uuid4()), field_mapping={}
        )
        downloader = DownloaderInstance(
            name="Synthetic", type="mock", url="http://example.invalid", download_dir="/tmp"
        )
        collection = WorkCollection(title_cn="Synthetic collection")
        db.add_all([channel, downloader, collection])
        await db.flush()
        work = (
            Movie(title_cn="Synthetic")
            if kind == "movie"
            else TVSeries(title_cn="Synthetic", collection_id=collection.id, season_number=1)
        )
        agent = Agent(name="Synthetic", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=True)
        resource = FileResource(
            channel_id=channel.id,
            guid=str(uuid.uuid4()),
            title_raw="Synthetic",
            torrent_url="magnet:?xt=urn:btih:synthetic",
        )
        db.add_all([work, agent, resource])
        await db.commit()
        wid = work.id
        args = {kind + "_id": wid}
        if model is AgentWork:
            args["agent_id"] = agent.id
            args["content_type"] = "tv" if kind == "series" else "movie"
        elif model is ChannelRawTitleMapping:
            args.update(channel_id=channel.id, raw_title="Synthetic", search_title_key="synthetic")
        else:
            args.update(resource_id=resource.id, source="manual")
            if model is ResourceFileAssignment:
                args["file_path"] = "synthetic.mkv"
    endpoint = delete_movie if kind == "movie" else delete_series
    async with database.async_session_factory() as writer, database.async_session_factory() as deleter:
        row = model(**args)
        writer.add(row)
        if insert_first:
            await writer.flush()
            response = await asyncio.wait_for(endpoint(wid, deleter), 5)
            assert response.status_code == 409
            assert json.loads(response.body)["error"]["code"] == "INVALID_STATE"
            await writer.commit()
            response = await endpoint(wid, deleter)
            assert response.status_code == 409
            assert json.loads(response.body)["error"]["code"] == "DELETE_BLOCKED"
        else:
            entered, release = asyncio.Event(), asyncio.Event()
            original = deletion.manual_deletion_references

            async def paused(*args):
                refs = await original(*args)
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
                return refs

            with writer.no_autoflush:
                pid = await writer.scalar(text("SELECT pg_backend_pid()"))
            with patch.object(deletion, "manual_deletion_references", paused):
                deletion_task = asyncio.create_task(endpoint(wid, deleter))
                await asyncio.wait_for(entered.wait(), 5)
                insertion_task = asyncio.create_task(writer.flush())
                async with database.async_session_factory() as observer:
                    for _ in range(100):
                        await observer.rollback()
                        waiting = await observer.scalar(
                            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}
                        )
                        if waiting == "Lock":
                            break
                        await asyncio.sleep(0.02)
                    assert waiting == "Lock", waiting
                release.set()
                await asyncio.wait_for(deletion_task, 5)
                try:
                    await asyncio.wait_for(insertion_task, 5)
                except IntegrityError as exc:
                    assert getattr(exc.orig, "sqlstate", None) == "23503"
                    await writer.rollback()
                else:
                    raise AssertionError("reference to deleted work committed")
    async with database.async_session_factory() as db:
        current = await db.get(type(work), wid)
        assert (current is not None) == insert_first
    return {"kind": kind, "reference": model.__tablename__, "insert_first": insert_first, "passed": True}


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    results = [
        await case(kind, model, order)
        for kind in ("series", "movie")
        for model in (ResourceWorkLink, ResourceFileAssignment, ChannelRawTitleMapping, AgentWork)
        for order in (True, False)
    ]
    Path("/tmp/rssripple-v12-new-references-ho.json").write_text(json.dumps(results, indent=2) + "\n")
    print(f"{len(results)} cases passed")
    await database.engine.dispose()


asyncio.run(main())
