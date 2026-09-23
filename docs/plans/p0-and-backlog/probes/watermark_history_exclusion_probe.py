# ruff: noqa: E402
import asyncio
import os

assert os.environ.get("DATABASE_URL") == "sqlite+aioturso:///:memory:?isolation_level=DEFERRED"
import json
from datetime import date, timedelta
from pathlib import Path

import app.database as database
import app.models  # noqa: F401
from app.job_handlers import _handle_run_agent
from app.models.agent import Agent
from app.models.channel import Channel
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
    from app.api.v1.agents import _apply_backfill
    from app.services.agent_resource_requests import request_channel_resources

    async with database.async_session_factory() as db:
        resource = _make_resource(channel.id, movie_id=movies[0].id, season=None, episode=None, parsed_at=None)
        resource.created_at = now - timedelta(days=2)
        db.add(resource)
        await db.flush()
        current = await db.get(Agent, aid)
        await _apply_backfill(current, [], db)
        await db.commit()
        rid = resource.id
    before = await _handle_run_agent({"agent_id": aid})
    assert before["total_resources"] == 0
    async with database.async_session_factory() as db:
        # Candidate design under review: unconditional completion event uses B9.
        await request_channel_resources(db, channel.id, [rid])
        await db.commit()
    after = await _handle_run_agent({"agent_id": aid})
    result = {
        "baseline_empty_selection_run": before,
        "unconditional_completion_request_run": after,
        "historical_resource_id": rid,
        "scope": "hypothesis test of reusing existing B9 function unconditionally",
    }
    Path("/tmp/rssripple-v13-history-ib.json").write_text(json.dumps(result, indent=2) + "\n")
    print(result)
    assert after["dispatched"] == 0, "candidate fix violates explicitly empty historical selection"


asyncio.run(main())
