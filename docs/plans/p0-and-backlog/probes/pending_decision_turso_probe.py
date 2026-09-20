# ruff: noqa: E402
"""Synthetic candidates through actual production creation on two Turso connections."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "sqlite+aioturso"
assert url.database.startswith("/tmp/rssripple-v11-concurrency-")

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
    bootstrap = create_async_engine(str(url))
    database.apply_db_pragmas(bootstrap)
    async with bootstrap.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
        await conn.execute(text("PRAGMA journal_mode='mvcc'"))
    await bootstrap.dispose()
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
            ids = await asyncio.gather(*(database.retry_on_lock(lambda pair=pair: writer(pair)) for pair in pairs))
        async with database.async_session_factory() as db:
            rows = list(await db.scalars(select(PendingDecision).where(PendingDecision.agent_id == agent_id)))
        assert len(rows) == 1 and len(rows[0].candidates) == 4 and ids[0] == ids[1]
        checks = {}
        async with database.async_session_factory() as db:
            for label, key in [("duplicate_key", rows[0].decision_key), ("null_pending_key", None)]:
                try:
                    async with db.begin_nested():
                        db.add(
                            PendingDecision(
                                agent_id=agent_id,
                                movie_id=movie_id,
                                decision_key=key,
                                decision_scope=rows[0].decision_scope,
                                candidates=rows[0].candidates,
                                reason="Synthetic constraint probe",
                                status="pending",
                            )
                        )
                        await db.flush()
                except IntegrityError:
                    checks[label] = True
                else:
                    checks[label] = False
            historical = PendingDecision(
                agent_id=agent_id,
                movie_id=movie_id,
                candidates=pairs[0],
                reason="Synthetic historical decision",
                status="decided",
            )
            db.add(historical)
            await db.commit()
        assert all(checks.values())
        result = {
            "constraint_checks": checks,
            "historical_unkeyed_nonpending_preserved": True,
            "initial_barrier_reached": both.is_set(),
            "picker_invocations_including_retries": entered,
            "created_ids": ids,
            "pending_rows": len(rows),
            "candidate_counts": [len(row.candidates) for row in rows],
            "scope": (
                "synthetic data; two real Turso connections; "
                "real production create and commits; LLM replaced by barrier"
            ),
        }
        Path("/tmp/rssripple-v11-turso-bw-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        assert len(rows) == 1, "Expected one merged pending decision"
    finally:
        await database.engine.dispose()


asyncio.run(main())
