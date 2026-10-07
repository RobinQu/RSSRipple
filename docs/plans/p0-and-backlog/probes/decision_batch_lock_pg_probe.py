# ruff: noqa: E402
"""Two real PG connections; synthetic candidates and controlled model latency."""

import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import text, update
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
assert (url.username, url.password, url.database) == ("organize_test",) * 3

import app.database as database
import app.models  # noqa: F401
from app.api.v1.decisions import batch_decisions
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.schemas.pending_decision import BatchDecisionRequest
from app.services.decision_store import choice_identity
from app.services.required_fields import normalize_required_fields
from app.utils.time import utcnow
from tests.unit.test_agent_service import _make_resource


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    async with database.async_session_factory() as db:
        channel = Channel(
            name="Synthetic status race",
            url="https://example.invalid/rss",
            field_mapping={},
            required_metadata_fields=normalize_required_fields(None),
        )
        downloader = DownloaderInstance(
            name="Synthetic", type="mock", url="http://example.invalid", download_dir="/tmp"
        )
        movie = Movie(title_cn="Synthetic movie", release_date=date(2020, 1, 1), is_anime=False)
        db.add_all([channel, downloader, movie])
        await db.flush()
        agent = Agent(name="Synthetic", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=True)
        resources = [
            _make_resource(channel.id, movie_id=movie.id, season=None, episode=None, parsed_at=None) for _ in range(2)
        ]
        db.add_all([agent, *resources])
        await db.flush()
        key, scope = choice_identity("movie", movie.id, None, None)
        decision = PendingDecision(
            agent_id=agent.id,
            movie_id=movie.id,
            candidates=[r.id for r in resources],
            reason="Synthetic two equivalent candidates",
            status="pending",
            decision_key=key,
            decision_scope=scope,
            expires_at=utcnow() + timedelta(days=1),
        )
        db.add(decision)
        await db.commit()
        did = decision.id
    async with database.async_session_factory() as db:
        second_movie = Movie(title_cn="Second synthetic movie", release_date=date(2020, 1, 1), is_anime=False)
        db.add(second_movie)
        await db.flush()
        second_resources = [
            _make_resource(channel.id, movie_id=second_movie.id, season=None, episode=None, parsed_at=None)
            for _ in range(2)
        ]
        db.add_all(second_resources)
        await db.flush()
        key, scope = choice_identity("movie", second_movie.id, None, None)
        second = PendingDecision(
            agent_id=agent.id,
            movie_id=second_movie.id,
            candidates=[r.id for r in second_resources],
            reason="Synthetic second choice",
            status="pending",
            decision_key=key,
            decision_scope=scope,
            expires_at=utcnow() + timedelta(days=1),
        )
        db.add(second)
        await db.commit()
        second_id = second.id
    changed = False
    model_calls = 0

    async def delayed_model(current_agent, candidates, key):
        nonlocal changed, model_calls
        model_calls += 1
        if model_calls == 2:
            async with database.async_session_factory() as other:
                await other.execute(text("SET LOCAL lock_timeout = '1500ms'"))
                await other.execute(update(Agent).where(Agent.id == agent.id).values(name="Concurrent update"))
                await other.commit()
            changed = True
        return candidates[0].id, "Synthetic model choice"

    dispatch = AsyncMock()
    try:
        async with database.async_session_factory() as db:
            decision = await db.get(PendingDecision, did)
            with (
                patch("app.services.agent_service._generate_llm_pick", delayed_model),
                patch("app.services.agent_service.dispatch_download", dispatch),
            ):
                response = await batch_decisions(
                    agent.id, BatchDecisionRequest(action="ai", decision_ids=[did, second_id]), db
                )
        result = {
            "other_transaction_updated_agent_during_second_model": changed,
            "dispatch_calls": dispatch.await_count,
            "model_calls": model_calls,
            "response": response,
        }
        Path("/tmp/rssripple-v11-batch-lock-cs-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        assert changed and dispatch.await_count == 2 and response["data"]["failed"] == 0
    finally:
        await database.engine.dispose()


asyncio.run(main())
