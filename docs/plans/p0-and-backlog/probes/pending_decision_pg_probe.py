# ruff: noqa: E402
"""Synthetic candidates through actual production creation on two PG connections."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
assert (url.username, url.password, url.database) == ("organize_test",) * 3
import app.database as database
import app.models  # noqa: F401
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.services.agent_service import create_pending_decision
from tests.unit.test_agent_service import _make_resource


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    async with database.async_session_factory() as db:
        channel = Channel(name="Synthetic decision race", url="https://example.invalid/rss", field_mapping={})
        movie = Movie(title_cn="Synthetic race movie")
        downloader = DownloaderInstance(
            name="Synthetic downloader", type="mock", url="http://example.invalid", download_dir="/tmp/synthetic"
        )
        db.add_all([channel, movie, downloader])
        await db.flush()
        agent = Agent(
            name="Synthetic race agent", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=True
        )
        candidates = [
            _make_resource(channel.id, movie_id=movie.id, episode=None, season=None, parsed_at=None) for _ in range(4)
        ]
        db.add_all([agent, *candidates])
        await db.commit()
        agent_id, movie_id = agent.id, movie.id
        pairs = [[c.id for c in candidates[:2]], [c.id for c in candidates[2:]]]
    entered = 0
    both = asyncio.Event()

    async def picker(*args):
        nonlocal entered
        entered += 1
        if entered == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 10)
        return None, None

    async def writer(ids):
        from app.models.file_resource import FileResource

        async with database.async_session_factory() as db:
            a = await db.get(Agent, agent_id)
            resources = list(await db.scalars(select(FileResource).where(FileResource.id.in_(ids))))
            row = await create_pending_decision(a, ("movie", movie_id, None), resources, db)
            await db.commit()
            return row.id

    try:
        with patch("app.services.agent_service._suggest_pick", picker):
            ids = await asyncio.gather(*(writer(pair) for pair in pairs))
        async with database.async_session_factory() as db:
            rows = list(await db.scalars(select(PendingDecision).where(PendingDecision.agent_id == agent_id)))
        result = {
            "both_creators_observed_empty_slot": entered == 2,
            "created_ids": ids,
            "pending_rows": len(rows),
            "candidate_counts": [len(row.candidates) for row in rows],
            "scope": ("synthetic data; two real PostgreSQL connections; "
                      "real production create and commits; LLM replaced by barrier"),
        }
        Path("/tmp/rssripple-v11-pg-bi-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        assert len(rows) == 2, "Reproduction changed: inspect actual result"
    finally:
        await database.engine.dispose()


asyncio.run(main())
