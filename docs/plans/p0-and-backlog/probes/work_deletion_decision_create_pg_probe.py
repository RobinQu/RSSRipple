# ruff: noqa: E402
import asyncio
import os

from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
assert (url.username, url.password, url.database) == ("organize_test",) * 3
import json
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

import app.database as database
import app.models  # noqa: F401
import app.services.work_deletion as deletion
from app.api.v1.movies import delete_movie
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.services.agent_service import create_pending_decision
from tests.unit.test_agent_service import _make_resource


async def case(create_first):
    async with database.async_session_factory() as db:
        channel = Channel(name="Synthetic deletion order", url="https://example.invalid/rss", field_mapping={})
        downloader = DownloaderInstance(
            name="Synthetic", type="mock", url="http://example.invalid", download_dir="/tmp"
        )
        work = Movie(title_cn="Synthetic deletion target")
        db.add_all([channel, downloader, work])
        await db.flush()
        agent = Agent(name="Synthetic", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=True)
        resources = [
            _make_resource(channel.id, movie_id=work.id, season=None, episode=None, parsed_at=None) for _ in range(2)
        ]
        db.add_all([agent, *resources])
        await db.commit()
        aid, wid = agent.id, work.id
    async with database.async_session_factory() as creator, database.async_session_factory() as deleter:
        if create_first:
            await create_pending_decision(agent, ("movie", wid, None), resources, creator, skip_llm=True)
            response = await asyncio.wait_for(delete_movie(wid, deleter), 5)
            assert response.status_code == 409
            await creator.commit()
            await asyncio.wait_for(delete_movie(wid, deleter), 5)
        else:
            entered, release = asyncio.Event(), asyncio.Event()
            real_check = deletion.manual_deletion_references

            async def paused_check(*args):
                refs = await real_check(*args)
                entered.set()
                await asyncio.wait_for(release.wait(), 5)
                return refs

            with patch.object(deletion, "manual_deletion_references", paused_check):
                task = asyncio.create_task(delete_movie(wid, deleter))
                await asyncio.wait_for(entered.wait(), 5)
                try:
                    await create_pending_decision(agent, ("movie", wid, None), resources, creator, skip_llm=True)
                except OperationalError as exc:
                    assert "concurrent decision identity change" in str(exc)
                    await creator.rollback()
                else:
                    raise AssertionError("creation accepted while deletion owns identity")
                release.set()
                await asyncio.wait_for(task, 5)
            try:
                await create_pending_decision(agent, ("movie", wid, None), resources, creator, skip_llm=True)
            except ValueError as exc:
                assert "changed identity" in str(exc)
                await creator.rollback()
            else:
                raise AssertionError("stale creation accepted after deletion")
    async with database.async_session_factory() as db:
        assert await db.get(Movie, wid) is None
        rows = list(await db.scalars(select(PendingDecision).where(PendingDecision.agent_id == aid)))
        assert not any(row.status == "pending" for row in rows)
        assert len(rows) == (1 if create_first else 0)
        if rows:
            assert rows[0].status == "expired"
    return {"create_first": create_first, "stale_pending": False, "history_rows": len(rows)}


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    results = [await case(order) for order in (True, False)]
    Path("/tmp/rssripple-v12-decision-create-hg.json").write_text(json.dumps(results, indent=2) + "\n")
    print(results)
    await database.engine.dispose()


asyncio.run(main())
