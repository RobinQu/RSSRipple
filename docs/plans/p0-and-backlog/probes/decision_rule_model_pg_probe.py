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
from app.api.v1.decisions import _ai_pick_and_dispatch
from app.models.agent import Agent
from app.models.agent_work import AgentWork
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.services.decision_store import choice_identity
from app.services.required_fields import normalize_required_fields
from app.utils.time import utcnow
from tests.unit.test_agent_service import _make_resource


async def main():
    mode = os.environ["MUTATION"]
    assert mode in {"channel", "subscription", "api_create", "api_update", "api_delete"}
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
        agent = Agent(name="Synthetic", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=False)
        resources = [
            _make_resource(channel.id, movie_id=movie.id, season=None, episode=None, parsed_at=None) for _ in range(2)
        ]
        db.add_all([agent, *resources])
        await db.flush()
        subscription = AgentWork(agent_id=agent.id, movie_id=movie.id, content_type="movie")
        db.add(subscription)
        await db.flush()
        subscription_id, channel_id = subscription.id, channel.id
        agent_id, movie_id = agent.id, movie.id
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
        picked = resources[0].id

    async def mutate(other):
        from app.api.v1.agents import create_work, delete_work, update_work
        from app.schemas.agent import AgentWorkCreate, AgentWorkUpdate

        if mode == "api_create":
            await create_work(agent_id, AgentWorkCreate(content_type="movie", movie_id=movie_id), other)
            return
        if mode == "api_update":
            await update_work(
                agent_id,
                subscription_id,
                AgentWorkUpdate(
                    filter_overrides={
                        "combinator": "and",
                        "conditions": [{"field": "resolution", "operator": "eq", "value": "4K"}],
                    }
                ),
                other,
            )
            return
        if mode == "api_delete":
            await delete_work(agent_id, subscription_id, other)
            return
        if mode == "channel":
            await other.execute(
                update(Channel)
                .where(Channel.id == channel_id)
                .values(required_metadata_fields=normalize_required_fields(["subtitle_type"]))
            )
        else:
            await other.execute(
                update(AgentWork)
                .where(AgentWork.id == subscription_id)
                .values(
                    filter_overrides={
                        "combinator": "and",
                        "conditions": [{"field": "resolution", "operator": "eq", "value": "4K"}],
                    }
                )
            )

    async def delayed_model(*args):
        async with database.async_session_factory() as other:
            await other.execute(text("SET LOCAL lock_timeout = '300ms'"))
            await mutate(other)
            await other.commit()
        return picked, "Synthetic response after rule edit committed"

    dispatch = AsyncMock()
    try:
        async with database.async_session_factory() as db:
            decision = await db.get(PendingDecision, did)
            with (
                patch("app.services.agent_service._generate_llm_pick", delayed_model),
                patch("app.services.agent_service.dispatch_download", dispatch),
            ):
                ok, error = await _ai_pick_and_dispatch(decision, db)
                await db.commit()
        async with database.async_session_factory() as db:
            final = await db.get(PendingDecision, did)
            result = {
                "mutation": mode,
                "model_edit_committed": True,
                "dispatch_calls": dispatch.await_count,
                "ok": ok,
                "error": error,
                "final_status": final.status,
            }
        Path(os.environ["PROBE_RESULT"]).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        assert not ok and dispatch.await_count == 0 and final.status == "pending"
    finally:
        await database.engine.dispose()


asyncio.run(main())
