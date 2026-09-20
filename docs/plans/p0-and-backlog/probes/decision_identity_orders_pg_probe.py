# ruff: noqa: E402
"""Actual creation/merge ordering on PG; synthetic inputs, controlled LLM wait."""

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
from app.services.decision_store import choice_identity
from app.services.resource_coverage import load_batch_coverage

import app.database as database
import app.models  # noqa: F401
import app.services.agent_service as service
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.services.metadata_dedup import DedupReport, _merge_movie_group
from tests.unit.test_agent_service import _make_resource


async def case(mode):
    linked = mode.startswith("links_")
    create_first = mode.endswith("create_first")
    async with database.async_session_factory() as db:
        channel = Channel(name="Synthetic order", url="https://example.invalid/rss", field_mapping={})
        downloader = DownloaderInstance(
            name="Synthetic", type="mock", url="http://example.invalid", download_dir="/tmp"
        )
        source, target = Movie(title_cn="Synthetic source"), Movie(title_cn="Synthetic target")
        second = Movie(title_cn="Synthetic unchanged second movie")
        db.add_all([channel, downloader, source, target, second])
        await db.flush()
        agent = Agent(name="Synthetic", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=True)
        resources = [
            _make_resource(channel.id, movie_id=source.id, season=None, episode=None, parsed_at=None) for _ in range(2)
        ]
        if linked:
            for resource in resources:
                resource.movie_id = None
                resource.is_batch = True
                resource.batch_scope = "movies"
                resource.work_links = [ResourceWorkLink(movie_id=w.id) for w in (source, second)]
                resource.file_assignments = [
                    ResourceFileAssignment(movie_id=w.id, file_path=f"{w.id}.mkv") for w in (source, second)
                ]
        db.add_all([agent, *resources])
        await db.commit()
        aid, sid, tid, rids = agent.id, source.id, target.id, [r.id for r in resources]
        second_id = second.id
    ready, attempted, committed, merged = (asyncio.Event() for _ in range(4))
    events = []

    async def do_merge():
        async with database.async_session_factory() as db:
            source, target = await db.get(Movie, sid), await db.get(Movie, tid)
            await _merge_movie_group(db, [target, source], DedupReport(), survivor=target)
            await db.commit()

    async def merge():
        await ready.wait()
        if create_first:
            try:
                await do_merge()
            except OperationalError as exc:
                assert "decision identity change" in str(exc)
                events.append("merge_busy")
            else:
                raise AssertionError("Merge accepted uncommitted creator")
            attempted.set()
            await committed.wait()
        await do_merge()
        events.append("merged")
        merged.set()

    async def delayed_model(*args):
        ready.set()
        await merged.wait()
        events.append("model_returned_after_merge")
        return rids[0], "Synthetic recommendation before identity change"

    async def create_choice(db, agent, resources, target_id, *, skip_llm):
        if linked:
            await load_batch_coverage(db, resources)
            assert all(r.movie_id is None and r.series_id is None for r in resources)
            return await service.create_pending_decision(
                agent,
                ("series", None, None, -1),
                resources,
                db,
                coverage=service._batch_coverage_key(resources[0]),
                skip_llm=skip_llm,
            )
        return await service.create_pending_decision(
            agent,
            ("movie", target_id, None),
            resources,
            db,
            skip_llm=skip_llm,
        )

    async def create():
        async with database.async_session_factory() as db:
            agent = await db.get(Agent, aid)
            resources = list(await db.scalars(select(FileResource).where(FileResource.id.in_(rids))))
            if create_first:
                await create_choice(db, agent, resources, sid, skip_llm=True)
                ready.set()
                await attempted.wait()
                await db.commit()
                committed.set()
            else:
                with patch.object(service, "_suggest_pick", delayed_model):
                    try:
                        await create_choice(db, agent, resources, sid, skip_llm=False)
                    except ValueError as exc:
                        assert "changed identity" in str(exc)
                        events.append("stale_model_identity_rejected")
                    else:
                        raise AssertionError("Model wrote stale work identity")
        if not create_first:
            async with database.async_session_factory() as db:
                agent = await db.get(Agent, aid)
                resources = list(await db.scalars(select(FileResource).where(FileResource.id.in_(rids))))
                await create_choice(db, agent, resources, tid, skip_llm=True)
                await db.commit()

    await asyncio.wait_for(asyncio.gather(create(), merge()), 15)
    async with database.async_session_factory() as db:
        rows = list(await db.scalars(select(PendingDecision).where(PendingDecision.agent_id == aid)))
        pending = [r for r in rows if r.status == "pending"]
        if linked:
            descriptors = tuple(sorted(("movie", identity, None, ()) for identity in (tid, second_id)))
            key, scope = choice_identity("series", None, None, -1, ("movies", descriptors))
            assert pending[0].series_id is None and pending[0].movie_id is None
        else:
            key, scope = choice_identity("movie", tid, None, None)
        assert len(pending) == 1
        assert pending[0].decision_key == key and pending[0].decision_scope == scope
        assert set(pending[0].candidates) == set(rids)
        assert pending[0].llm_picked_resource_id is None
        assert sum(r.status == "expired" for r in rows) == (1 if create_first else 0)
        assert await db.get(Movie, sid) is None
    return dict(mode=mode, events=events, pending_count=1, candidates_preserved=True, old_recommendation_absent=True)


async def main():
    try:
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)
        results = [
            await case(mode) for mode in ("create_first", "model_wait", "links_create_first", "links_model_wait")
        ]
        Path(os.environ["PROBE_RESULT"]).write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results))
    finally:
        await database.engine.dispose()


asyncio.run(main())
