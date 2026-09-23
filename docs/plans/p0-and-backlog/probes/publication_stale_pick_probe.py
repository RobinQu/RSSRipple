# ruff: noqa: E402
import asyncio
import os

assert os.environ.get("DATABASE_URL") == "sqlite+aioturso:///:memory:?isolation_level=DEFERRED"
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from app.services.agent_publication_progress import reset_progress
from app.services.resource_publication import publish_resource
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
        earlier = _make_resource(channel.id, movie_id=movies[1].id, season=None, episode=None, parsed_at=None)
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
        from app.api.v1.agents import _apply_backfill

        async with database.async_session_factory() as saved:
            current = await saved.get(Agent, aid)
            await _apply_backfill(current, [], saved)
            await saved.commit()
        cutoff = None if os.environ.get("WINDOW_ALL") == "1" else (now - timedelta(minutes=3)).isoformat()
        from app.services import agent_service

        async def reset_during_pick(agent, candidates, key):
            async with database.async_session_factory() as changed:
                current = await changed.get(Agent, aid)
                await _apply_backfill(current, [], changed)
                await changed.commit()
            return candidates[0].id, "Synthetic pick after actual backfill"

        with patch.object(agent_service, "_generate_llm_pick", side_effect=reset_during_pick) as pick:
            stale = await _handle_run_agent({"agent_id": aid, "scan_since": cutoff})
            assert pick.await_count == 1
    async with database.async_session_factory() as db:
        tasks = list(await db.scalars(select(DownloadTask)))
    result = {
        "stale_run": stale,
        "task_count": len(tasks),
        "scope": "actual backfill commits during candidate pick before downloader RPC",
    }
    Path(os.environ["PROBE_RESULT_PATH"]).write_text(json.dumps(result, indent=2) + "\n")
    assert stale["dispatched"] == 0 and not tasks, "old run dispatched history after explicit exclusion committed"


asyncio.run(main())
