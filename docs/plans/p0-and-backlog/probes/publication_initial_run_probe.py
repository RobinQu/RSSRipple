# ruff: noqa: E402
import asyncio
import os

assert os.environ.get("DATABASE_URL") == "sqlite+aioturso:///:memory:?isolation_level=DEFERRED"
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.resource_publication import publish_resource
from sqlalchemy import select

import app.database as database
import app.models  # noqa: F401
import app.services.fetch_service as fetch_service
from app.job_handlers import _handle_run_agent
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.movie import Movie
from app.utils.time import utcnow
from tests.unit.test_agent_service import _make_resource


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    now = utcnow()
    async with database.async_session_factory() as db:
        channel = Channel(
            name="Synthetic watermark race",
            url="https://example.invalid/rss",
            field_mapping={},
            metadata_agent_enabled=False,
        )
        downloader = DownloaderInstance(
            name="Synthetic", type="mock", url="http://example.invalid", download_dir="/tmp"
        )
        movies = [Movie(title_cn=f"Synthetic {i}", release_date=date(2020, 1, 1), is_anime=False) for i in range(2)]
        db.add_all([channel, downloader, *movies])
        await db.flush()
        agent = Agent(
            name="Synthetic watermark",
            channel_id=channel.id,
            downloader_id=downloader.id,
            scope_channel_wide=True,
            last_consumed_at=None,
        )
        db.add(agent)
        await db.flush()
        await db.commit()
        aid = agent.id
    initial = await _handle_run_agent({"agent_id": aid})
    assert initial["total_resources"] == 0 and not initial["errors"], initial
    async with database.async_session_factory() as late:
        earlier = _make_resource(channel.id, movie_id=None, season=None, episode=None, parsed_at=None)
        earlier.created_at = now - timedelta(minutes=2)
        late.add(earlier)
        await late.flush()
        await publish_resource(late, earlier.id, kind="created")
        newer = _make_resource(channel.id, movie_id=movies[1].id, season=None, episode=None, parsed_at=None)
        newer.created_at = now - timedelta(minutes=1)
        late.add(newer)
        await late.flush()
        await publish_resource(late, newer.id, kind="created")
        await late.commit()
        first = await _handle_run_agent({"agent_id": aid})
        assert not first["errors"] and first["unrecognized"] == 1, first

        async def synthetic_source_link(db, resource, channel):
            resource.movie_id = movies[0].id

        with (
            patch.object(fetch_service, "fetch_and_link_metadata", synthetic_source_link),
            patch("app.services.torrent_inspect.ensure_torrent_cached", AsyncMock()),
            patch("app.services.torrent_inspect.maybe_inspect_torrent", AsyncMock()),
            patch("app.services.magnet_resolve.enqueue_resolution", AsyncMock()),
        ):
            await fetch_service._process_resource_metadata(earlier.id, channel.id, asyncio.Semaphore(1))

    second = await _handle_run_agent({"agent_id": aid})
    third = await _handle_run_agent({"agent_id": aid})
    async with database.async_session_factory() as db:
        tasks = list(await db.scalars(select(DownloadTask)))
        agent = await db.get(Agent, aid)
        result = {
            "initial": initial,
            "first": first,
            "second": second,
            "third": third,
            "older_resource_id": earlier.id,
            "newer_resource_id": newer.id,
            "task_resource_ids": [t.file_resource_id for t in tasks],
            "watermark": str(agent.last_consumed_at),
            "older_created_at": str(earlier.created_at),
        }
    control = await _handle_run_agent({"agent_id": aid, "resource_ids": [earlier.id]})
    assert control["dispatched"] == 0 and not control["errors"], control
    assert second["dispatched"] == 1 and not second["errors"], second
    assert third["total_resources"] == 0, third
    result["targeted_control"] = control
    Path("/tmp/rssripple-v13-initial-ij.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(result)
    assert earlier.id in result["task_resource_ids"], "metadata-completed eligible resource skipped by incremental runs"


asyncio.run(main())
