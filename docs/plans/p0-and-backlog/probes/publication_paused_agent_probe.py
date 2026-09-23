# ruff: noqa: E402
import asyncio
import os

assert os.environ.get("DATABASE_URL") == "sqlite+aioturso:///:memory:?isolation_level=DEFERRED"
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
from app.services.resource_publication import publish_resource
from app.services.agent_publication_progress import reset_progress
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
            last_consumed_at=now - timedelta(days=1),
        )
        db.add(agent)
        await db.flush()
        await reset_progress(db, agent.id, channel.id)
        await db.commit()
        aid = agent.id
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
        from app.api.v1.agents import update_agent
        from app.schemas.agent import AgentUpdate
        from app.services.agent_publication_progress import snapshot_publications
        async with database.async_session_factory() as changed:
            await update_agent(aid, AgentUpdate(status="paused"), changed)
        async with database.async_session_factory() as observer:
            before = await snapshot_publications(observer, aid, channel.id)
        paused = await _handle_run_agent({"agent_id": aid, "automatic": True})
        async with database.async_session_factory() as observer:
            after = await snapshot_publications(observer, aid, channel.id)
            tasks = list(await observer.scalars(select(DownloadTask)))
        result = {"paused_run": paused, "task_count_while_paused": len(tasks), "pending_before": list(before.resource_ids), "pending_after": list(after.resource_ids)}
        Path(os.environ["PROBE_RESULT_PATH"]).write_text(json.dumps(result, indent=2)+"\n")
        assert paused.get("status") == "skipped" and not tasks and before == after, "automatic queued run consumed paused Agent"
        async with database.async_session_factory() as changed:
            await update_agent(aid, AgentUpdate(status="active"), changed)
        resumed = await _handle_run_agent({"agent_id": aid, "automatic": True})
        assert resumed["dispatched"] == 1 and not resumed["errors"], resumed
        result["resumed_run"] = resumed
        Path(os.environ["PROBE_RESULT_PATH"]).write_text(json.dumps(result, indent=2)+"\n")


asyncio.run(main())
