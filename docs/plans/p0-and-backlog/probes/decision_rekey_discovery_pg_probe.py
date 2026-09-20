# ruff: noqa: E402
"""Real PG empty-slot discovery race; synthetic works, no network/LLM calls."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
assert (url.username, url.password, url.database) == ("organize_test",) * 3

import app.services.decision_rekey as rekey
from app.services.decision_store import choice_identity

import app.database as database
import app.models  # noqa: F401
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.services.agent_service import create_pending_decision
from app.services.metadata_dedup import DedupReport, _merge_movie_group
from tests.unit.test_agent_service import _make_resource


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    async with database.async_session_factory() as db:
        channel = Channel(name="Synthetic discovery", url="https://example.invalid/rss", field_mapping={})
        downloader = DownloaderInstance(
            name="Synthetic", type="mock", url="http://example.invalid", download_dir="/tmp"
        )
        source, target = Movie(title_cn="Synthetic source"), Movie(title_cn="Synthetic target")
        db.add_all([channel, downloader, source, target])
        await db.flush()
        agent = Agent(name="Synthetic", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=True)
        resources = [
            _make_resource(channel.id, movie_id=source.id, season=None, episode=None, parsed_at=None) for _ in range(2)
        ]
        db.add_all([agent, *resources])
        await db.commit()
        aid, sid, tid = agent.id, source.id, target.id
        rids = [r.id for r in resources]
    discovered, created, merged = asyncio.Event(), asyncio.Event(), asyncio.Event()
    guarded = os.environ.get("GUARDED") == "1"
    rejected = []
    original = rekey.lock_work_choice_agents
    observed = []

    async def discover_then_pause(db, groups):
        ids = await original(db, groups)
        observed.extend(ids)
        discovered.set()
        await created.wait()
        return ids

    async def merge():
        async with database.async_session_factory() as db:
            source, target = await db.get(Movie, sid), await db.get(Movie, tid)
            await _merge_movie_group(db, [target, source], DedupReport(), survivor=target)
            await db.commit()
        merged.set()

    async def create():
        await discovered.wait()
        try:
            async with database.async_session_factory() as db:
                agent = await db.get(Agent, aid)
                resources = list(await db.scalars(select(FileResource).where(FileResource.id.in_(rids))))
                await create_pending_decision(agent, ("movie", sid, None), resources, db, skip_llm=True)
                await db.commit()
        except OperationalError as exc:
            if not guarded or "decision identity change" not in str(exc):
                raise
            rejected.append("coordination_busy")
        finally:
            created.set()
        if guarded:
            await merged.wait()
            # A stale key after the merge must also be rejected, even though
            # the coordination lock is free now. Then regroup from current data.
            async with database.async_session_factory() as db:
                agent = await db.get(Agent, aid)
                resources = list(await db.scalars(select(FileResource).where(FileResource.id.in_(rids))))
                try:
                    await create_pending_decision(agent, ("movie", sid, None), resources, db, skip_llm=True)
                except ValueError as exc:
                    assert "changed identity" in str(exc)
                    rejected.append("stale_identity")
                else:
                    raise AssertionError("Stale identity was accepted after merge")
            async with database.async_session_factory() as db:
                agent = await db.get(Agent, aid)
                resources = list(await db.scalars(select(FileResource).where(FileResource.id.in_(rids))))
                assert all(r.movie_id == tid for r in resources)
                await create_pending_decision(agent, ("movie", tid, None), resources, db, skip_llm=True)
                await db.commit()

    try:
        with patch.object(rekey, "lock_work_choice_agents", discover_then_pause):
            await asyncio.wait_for(asyncio.gather(merge(), create()), 15)
        async with database.async_session_factory() as db:
            rows = list(await db.scalars(select(PendingDecision).where(PendingDecision.status == "pending")))
            expected_key, expected_scope = choice_identity("movie", tid, None, None)
            valid = len(rows) == 1 and rows[0].decision_key == expected_key and rows[0].decision_scope == expected_scope
            result = dict(
                rejections=rejected,
                discovered_agents=observed,
                pending=[dict(id=r.id, movie_id=r.movie_id, scope=r.decision_scope) for r in rows],
                canonical_after_merge=valid,
            )
        Path(os.environ["PROBE_RESULT"]).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        assert valid, "New choice escaped work-rekey discovery"
        if guarded:
            assert rejected == ["coordination_busy", "stale_identity"]
    finally:
        await database.engine.dispose()


asyncio.run(main())
