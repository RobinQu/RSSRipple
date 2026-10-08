"""Actual handler history, B9 requests and late-result fencing on both DBs."""

import asyncio

import pytest
from sqlalchemy import delete, select, update

from app.job_handlers import _handle_run_agent
from app.models.agent import Agent
from app.models.agent_resource_request import AgentResourceRequest
from app.models.agent_run import AgentRun
from app.models.agent_run_lease import AgentRunLease
from app.services import agent_run_execution, agent_service
from app.services.agent_resource_requests import request_resources
from app.services.agent_run_lifecycle import reap_expired_runs
from app.services.task_queue import ExecutionOwnershipLostError


@pytest.fixture
async def handler_seed(lifecycle_db):
    _, factory, owner, agent_id, resource_id = lifecycle_db
    async with factory() as db:
        await db.execute(delete(AgentRun).where(AgentRun.id == owner.run_id))
        await db.execute(update(Agent).where(Agent.id == agent_id).values(scope_channel_wide=True))
        await request_resources(db, [agent_id], [resource_id])
        await db.commit()
    return factory, {"agent_id": agent_id, "resource_ids": [resource_id]}


async def state(factory, payload):
    async with factory() as db:
        runs = list(await db.scalars(select(AgentRun).where(AgentRun.agent_id == payload["agent_id"])))
        requests = list(await db.scalars(select(AgentResourceRequest).where(
            AgentResourceRequest.agent_id == payload["agent_id"],
        )))
        agent = await db.get(Agent, payload["agent_id"])
        leases = list(await db.scalars(select(AgentRunLease).where(
            AgentRunLease.run_id.in_([run.id for run in runs]),
        )))
        return runs, requests, agent, leases


async def test_actual_success_finalizes_history_and_request_together(handler_seed):
    factory, payload = handler_seed
    result = await _handle_run_agent(payload)
    runs, requests, agent, leases = await state(factory, payload)
    assert len(runs) == 1 and runs[0].id == result["run_id"]
    assert runs[0].status == agent.last_run_status == "success"
    assert runs[0].total_resources == runs[0].unrecognized == 1
    assert runs[0].dispatched == 0 and runs[0].finished_at is not None
    assert requests == leases == []


async def test_unexpected_exception_finalizes_and_defers_without_leaking_error(handler_seed, monkeypatch):
    factory, payload = handler_seed

    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic-private-value")

    monkeypatch.setattr(agent_service, "process_resources", fail)
    with pytest.raises(RuntimeError, match="synthetic-private-value"):
        await _handle_run_agent(payload)
    runs, requests, agent, leases = await state(factory, payload)
    assert len(runs) == len(requests) == 1
    assert runs[0].status == agent.last_run_status == "failed"
    assert runs[0].finished_at is not None and leases == []
    assert requests[0].attempt_count == 1 and requests[0].next_attempt_at is not None
    assert "RuntimeError" in runs[0].errors[0] and "synthetic-private-value" not in str(runs[0].errors)


@pytest.mark.parametrize("interruption", ["cancel", "revoke"])
async def test_interruption_keeps_requests_unacknowledged(handler_seed, monkeypatch, interruption):
    factory, payload = handler_seed
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return agent_service.RunResult(total_resources=1, unrecognized=1)

    monkeypatch.setattr(agent_service, "process_resources", paused)
    task = asyncio.create_task(_handle_run_agent(payload))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        if interruption == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            async with factory() as db:
                await db.execute(update(AgentRunLease).values(expires_at_epoch=0))
                await db.commit()
            assert len(await reap_expired_runs()) == 1
            release.set()
            with pytest.raises(ExecutionOwnershipLostError):
                await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    runs, requests, agent, leases = await state(factory, payload)
    assert len(runs) == len(requests) == 1 and leases == []
    assert runs[0].status == agent.last_run_status == "failed"
    assert runs[0].finished_at is not None and runs[0].total_resources == 0
    assert requests[0].attempt_count == 0


async def test_slow_handler_renews_beyond_initial_deadline(handler_seed, monkeypatch):
    factory, payload = handler_seed
    original = agent_service.process_resources
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(agent_run_execution, "LEASE_SECONDS", 2)
    monkeypatch.setattr(agent_run_execution, "HEARTBEAT_SECONDS", 0.2)

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(agent_service, "process_resources", slow)
    task = asyncio.create_task(_handle_run_agent(payload))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        initial = (await state(factory, payload))[3][0].expires_at_epoch
        await asyncio.sleep(2.2)
        current = (await state(factory, payload))[3][0].expires_at_epoch
        assert current > initial and await reap_expired_runs() == []
        release.set()
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    runs, requests, agent, leases = await state(factory, payload)
    assert runs[0].status == agent.last_run_status == "success" and requests == leases == []


async def test_older_failure_does_not_overwrite_newer_success(handler_seed, monkeypatch):
    factory, payload = handler_seed
    original = agent_service.process_resources
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def ordered(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("older attempt failed late")
        return await original(*args, **kwargs)

    monkeypatch.setattr(agent_service, "process_resources", ordered)
    old = asyncio.create_task(_handle_run_agent(payload))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        newer = await _handle_run_agent(payload)
        release.set()
        with pytest.raises(RuntimeError, match="failed late"):
            await asyncio.wait_for(old, timeout=10)
    finally:
        if not old.done():
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)
    runs, requests, agent, leases = await state(factory, payload)
    assert sorted(run.status for run in runs) == ["failed", "success"]
    assert next(run for run in runs if run.id == newer["run_id"]).status == "success"
    assert agent.last_run_status == "success" and requests == leases == []
