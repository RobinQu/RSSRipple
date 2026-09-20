# ruff: noqa: E402
import asyncio
import os

from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
assert (url.username, url.password, url.database) == ("organize_test",) * 3
import json
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select

import app.database as database
import app.models  # noqa: F401
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
        channel = Channel(name="Synthetic watermark race", url="https://example.invalid/rss", field_mapping={})
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
            last_consumed_at=now - timedelta(days=1),
        )
        db.add(agent)
        await db.commit()
        aid = agent.id
    async with database.async_session_factory() as late:
        earlier = _make_resource(channel.id, movie_id=movies[0].id, season=None, episode=None, parsed_at=None)
        earlier.created_at = now - timedelta(minutes=2)
        late.add(earlier)
        await late.flush()
        async with database.async_session_factory() as visible:
            newer = _make_resource(channel.id, movie_id=movies[1].id, season=None, episode=None, parsed_at=None)
            newer.created_at = now - timedelta(minutes=1)
            visible.add(newer)
            await visible.commit()
        first = await _handle_run_agent({"agent_id": aid})
        assert not first["errors"], first
        await late.commit()
    second = await _handle_run_agent({"agent_id": aid})
    third = await _handle_run_agent({"agent_id": aid})
    async with database.async_session_factory() as db:
        tasks = list(await db.scalars(select(DownloadTask)))
        agent = await db.get(Agent, aid)
        result = {
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
    assert control["dispatched"] == 1 and not control["errors"]
    result["targeted_control"] = control
    Path("/tmp/rssripple-v13-watermark-hy.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(result)
    assert earlier.id in result["task_resource_ids"], "late-committed eligible resource permanently skipped"


asyncio.run(main())
