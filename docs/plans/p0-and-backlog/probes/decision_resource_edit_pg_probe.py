# ruff: noqa: E402
"""Two real PG connections; synthetic candidates and controlled model latency."""

import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
assert (url.username, url.password, url.database) == ("organize_test",) * 3

from app.services.decision_store import choice_identity

import app.database as database
import app.models  # noqa: F401
from app.api.v1.decisions import _ai_pick_and_dispatch
from app.models.agent import Agent
from app.models.agent_resource_request import AgentResourceRequest
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
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
            llm_picked_resource_id=resources[0].id,
            decision_key=key,
            decision_scope=scope,
            expires_at=utcnow() + timedelta(days=1),
        )
        db.add(decision)
        await db.commit()
        did = decision.id
        picked = resources[0].id
    import app.api.v1.decisions as decisions_api
    import app.api.v1.resources as resources_api
    from app.schemas.file_resource import ResourceParseCorrectionRequest

    edited = asyncio.Event()
    confirmation_locked = asyncio.Event()
    original_request = resources_api.request_channel_resources
    original_lock = decisions_api._lock_current_decision
    observed = []

    async def request_after_edit(db, channel_id, resource_ids):
        edited.set()  # The real API has already flushed the resource UPDATE.
        await confirmation_locked.wait()
        return await original_request(db, channel_id, resource_ids)

    async def lock_and_signal(decision, db):
        locked = await original_lock(decision, db)
        confirmation_locked.set()  # Agent + decision locked; resource lock follows.
        return locked

    async def capture_dispatch(agent, resource, db):
        observed.append(resource.resolution)

    async def edit():
        async with database.async_session_factory() as db:
            result = await resources_api.correct_parse_fields(
                picked, ResourceParseCorrectionRequest(resolution="720p"), db
            )
            assert result["success"]

    async def confirm():
        await edited.wait()
        async with database.async_session_factory() as db:
            decision = await db.get(PendingDecision, did)
            ok, error = await _ai_pick_and_dispatch(decision, db)
            assert ok, error
            await db.commit()

    try:
        with (
            patch.object(resources_api, "request_channel_resources", request_after_edit),
            patch.object(resources_api, "_retry_torrent_cache", AsyncMock()),
            patch.object(resources_api, "wake_agents", AsyncMock()),
            patch.object(decisions_api, "_lock_current_decision", lock_and_signal),
            patch("app.services.agent_service.dispatch_download", capture_dispatch),
        ):
            await asyncio.wait_for(asyncio.gather(edit(), confirm()), timeout=10)
        async with database.async_session_factory() as db:
            requests = list(
                await db.scalars(select(AgentResourceRequest).where(AgentResourceRequest.resource_id == picked))
            )
            final = await db.get(PendingDecision, did)
            result = {
                "dispatch_resolutions": observed,
                "request_revisions": [r.revision for r in requests],
                "final_status": final.status,
                "editor_flushed_before_agent_lock": edited.is_set(),
                "confirmation_parent_lock_observed": confirmation_locked.is_set(),
            }
        Path(os.environ["PROBE_RESULT"]).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        assert observed == ["720p"] and len(requests) == 1 and final.status == "decided"
    finally:
        await database.engine.dispose()


asyncio.run(main())
