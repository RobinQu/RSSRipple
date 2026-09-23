# ruff: noqa: E402
import asyncio
import os

assert os.environ.get("DATABASE_URL") == "sqlite+aioturso:///:memory:?isolation_level=DEFERRED"
import json
from datetime import date, timedelta
from pathlib import Path

from app.services.agent_publication_progress import reset_progress
from app.services.resource_publication import publish_resource

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
        from app.api.v1.agents import _apply_backfill

        async with database.async_session_factory() as saved:
            current = await saved.get(Agent, aid)
            await _apply_backfill(current, [], saved)
            await saved.commit()
        cutoff = None if os.environ.get("WINDOW_ALL") == "1" else (now - timedelta(minutes=3)).isoformat()
        from sqlalchemy import event

        def fail_task_insert(conn, cursor, statement, parameters, context, executemany):
            if statement.lower().startswith("insert into download_tasks"):
                raise RuntimeError("Synthetic task persistence failure")

        event.listen(database.engine.sync_engine, "before_cursor_execute", fail_task_insert)
        try:
            failed = await _handle_run_agent({"agent_id": aid, "scan_since": cutoff})
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", fail_task_insert)
        assert failed["errors"], failed
    retried = await _handle_run_agent({"agent_id": aid})
    result = {
        "failed_window": failed,
        "ordinary_retry": retried,
        "data": "synthetic task INSERT failure; actual handler and transaction rollback",
    }
    Path(os.environ["PROBE_RESULT_PATH"]).write_text(json.dumps(result, indent=2) + "\n")
    assert retried["dispatched"] == 1 and not retried["errors"], (
        "failed explicit scan was lost to ordinary incremental retry"
    )


asyncio.run(main())
