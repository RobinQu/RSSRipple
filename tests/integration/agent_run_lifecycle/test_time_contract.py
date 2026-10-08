"""Run completion and lease reaping must retain UTC after the V32 rebase."""
import time
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, text, update

from app.job_handlers import _handle_run_agent
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_lease import AgentRunLease
from app.services.agent_resource_requests import request_resources
from app.services.agent_run_lifecycle import reap_expired_runs, renew_lease
from app.utils.time import utcnow


async def check_zone(pair, zone, action):
    engine, factory, owner, agent_id, resource_id = pair
    if engine.dialect.name == "postgresql":
        assert zone in {"UTC", "Asia/Shanghai", "America/New_York"}
        async with engine.begin() as conn:
            name = await conn.scalar(text("SELECT current_database()"))
            quote = conn.dialect.identifier_preparer.quote
            await conn.execute(text(f"ALTER DATABASE {quote(name)} SET timezone TO '{zone}'"))
        await engine.dispose()
        async with engine.connect() as conn:
            assert await conn.scalar(text("SHOW timezone")) == zone
    before = utcnow() - timedelta(seconds=1)
    if action == "finish":
        async with factory() as db:
            await db.execute(delete(AgentRun).where(AgentRun.id == owner.run_id))
            await db.execute(update(Agent).where(Agent.id == agent_id).values(scope_channel_wide=True))
            await request_resources(db, [agent_id], [resource_id])
            await db.commit()
        result = await _handle_run_agent({"agent_id": agent_id, "resource_ids": [resource_id]})
        run_id, expected = result["run_id"], "success"
    else:
        # Epoch authority must also be independent of a session's local clock.
        started = time.time()
        async with factory() as db:
            assert await renew_lease(db, owner)
            expiry = await db.scalar(select(AgentRunLease.expires_at_epoch).where(AgentRunLease.run_id == owner.run_id))
            assert started + 29 <= expiry <= time.time() + 31
            await db.execute(update(Agent).where(Agent.id == agent_id).values(current_run_token=owner.token))
            await db.execute(update(AgentRunLease).where(AgentRunLease.run_id == owner.run_id).values(expires_at_epoch=0))
            await db.commit()
        assert await reap_expired_runs() == [owner.run_id]
        run_id, expected = owner.run_id, "failed"
    after = utcnow()
    async with factory() as db:
        run = await db.get(AgentRun, run_id)
        agent = await db.get(Agent, agent_id)
        assert run.status == agent.last_run_status == expected
        assert run.finished_at.tzinfo is None and agent.last_run_at.tzinfo is None
        assert before <= run.finished_at <= after
        assert before <= agent.last_run_at <= after
        assert await db.scalar(select(AgentRunLease.id).where(AgentRunLease.run_id == run_id)) is None
        if action == "finish":
            assert before <= run.started_at <= after


@pytest.mark.parametrize("lifecycle_backend", ["work_fk_postgres"], indirect=True)
@pytest.mark.parametrize("zone", ["UTC", "Asia/Shanghai", "America/New_York"])
@pytest.mark.parametrize("action", ["finish", "reap"])
async def test_postgres_lifecycle_timestamps(lifecycle_db, zone, action):
    await check_zone(lifecycle_db, zone, action)


@pytest.mark.parametrize("lifecycle_backend", ["work_fk_turso"], indirect=True)
@pytest.mark.parametrize("action", ["finish", "reap"])
async def test_turso_lifecycle_timestamps(lifecycle_db, action):
    await check_zone(lifecycle_db, "UTC", action)
