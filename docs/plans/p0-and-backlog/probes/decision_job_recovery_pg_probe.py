# ruff: noqa: E402
"""Actual job handler, requests and watermark recovery after a work merge."""

import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
assert (url.username, url.password, url.database) == ("organize_test",) * 3
import app.database as database
import app.models  # noqa: F401
import app.services.agent_resource_requests as requests
import app.services.agent_service as service
from app.job_handlers import _handle_run_agent
from app.models.agent import Agent
from app.models.agent_resource_request import AgentResourceRequest
from app.models.agent_run import AgentRun
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.services.decision_store import choice_identity
from app.services.metadata_dedup import DedupReport, _merge_movie_group
from app.utils.time import utcnow
from tests.unit.test_agent_service import _make_resource


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    watermark = utcnow() - timedelta(days=1)
    request_clock = utcnow()
    async with database.async_session_factory() as db:
        channel = Channel(name="Synthetic job", url="https://example.invalid/rss", field_mapping={})
        downloader = DownloaderInstance(
            name="Synthetic", type="mock", url="http://example.invalid", download_dir="/tmp"
        )
        source, target = [
            Movie(title_cn=f"Synthetic {label}", release_date=date(2020, 1, 1), is_anime=False)
            for label in ("source", "target")
        ]
        db.add_all([channel, downloader, source, target])
        await db.flush()
        agent = Agent(
            name="Synthetic",
            channel_id=channel.id,
            downloader_id=downloader.id,
            scope_channel_wide=True,
            conflict_resolution="ask",
            last_consumed_at=watermark,
        )
        resources = [
            _make_resource(channel.id, movie_id=source.id, season=None, episode=None, parsed_at=None) for _ in range(2)
        ]
        db.add_all([agent, *resources])
        await db.flush()
        aid, sid, tid, rids = agent.id, source.id, target.id, [r.id for r in resources]
        await requests.request_resources(db, [aid], rids)
        await db.commit()
    model_ready, merged = asyncio.Event(), asyncio.Event()

    async def model(*args):
        model_ready.set()
        await merged.wait()
        return rids[0], "Synthetic stale recommendation"

    async def merge():
        await model_ready.wait()
        async with database.async_session_factory() as db:
            source, target = await db.get(Movie, sid), await db.get(Movie, tid)
            await _merge_movie_group(db, [target, source], DedupReport(), survivor=target)
            await db.commit()
        merged.set()

    try:
        with patch.object(requests, "utcnow", lambda: request_clock):
            with patch.object(service, "_suggest_pick", model):
                first, _ = await asyncio.wait_for(asyncio.gather(_handle_run_agent({"agent_id": aid}), merge()), 20)
            assert len(first["errors"]) == 1 and "changed identity" in first["errors"][0], first
            async with database.async_session_factory() as db:
                agent = await db.get(Agent, aid)
                rows = list(await db.scalars(select(AgentResourceRequest).where(AgentResourceRequest.agent_id == aid)))
                assert agent.last_consumed_at == watermark
                assert len(rows) == 2 and all(
                    r.attempt_count == 1 and r.next_attempt_at == request_clock + timedelta(seconds=30) for r in rows
                )
                assert await db.scalar(select(PendingDecision.id).where(PendingDecision.agent_id == aid)) is None
            request_clock += timedelta(seconds=31)
            # Production handler selects current resources and recomputes keys.
            # No process_resources/result stub or manual candidate regrouping.
            second = await _handle_run_agent({"agent_id": aid})
            assert not second["errors"] and second["pending_decisions"] == 1, second
            async with database.async_session_factory() as db:
                agent = await db.get(Agent, aid)
                assert agent.last_consumed_at > watermark
                assert (
                    await db.scalar(select(AgentResourceRequest.id).where(AgentResourceRequest.agent_id == aid)) is None
                )
                pending = list(
                    await db.scalars(
                        select(PendingDecision).where(
                            PendingDecision.agent_id == aid, PendingDecision.status == "pending"
                        )
                    )
                )
                assert len(pending) == 1
                key, scope = choice_identity("movie", tid, None, None)
                assert pending[0].decision_key == key and pending[0].decision_scope == scope
                assert set(pending[0].candidates) == set(rids)
                runs = list(
                    await db.scalars(select(AgentRun).where(AgentRun.agent_id == aid).order_by(AgentRun.started_at))
                )
                assert [r.status for r in runs] == ["failed", "pending_decisions"]
        result = dict(
            first_errors=first["errors"],
            second_pending=second["pending_decisions"],
            watermark_held_then_advanced=True,
            requests_deferred_then_acknowledged=True,
            run_statuses=[r.status for r in runs],
            candidates_preserved=True,
        )
        Path(os.environ["PROBE_RESULT"]).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        await database.engine.dispose()


asyncio.run(main())
