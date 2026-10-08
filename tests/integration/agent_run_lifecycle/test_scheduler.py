"""Production scheduler registration and execution with occupied queue capacity."""

import asyncio

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from sqlalchemy import select, update

from app.config import settings
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_lease import AgentRunLease
from app.services import scheduler, task_queue
from app.services.agent_run_lifecycle import _clock, create_lease, reap_expired_runs, renew_lease
from app.services.task_queue import MemoryQueue
from app.utils.time import utcnow


@pytest.mark.parametrize("scheduler_count", [1, 2])
async def test_registered_reaper_runs_with_full_queue(lifecycle_db, monkeypatch, scheduler_count):
    _, factory, owner, agent_id, _ = lifecycle_db
    async with factory() as db:
        assert await renew_lease(db, owner, seconds=1)
        await db.execute(update(Agent).where(Agent.id == agent_id).values(current_run_token=owner.token))
        live = AgentRun(agent_id=agent_id, status="running")
        legacy = AgentRun(agent_id=agent_id, status="running")
        db.add_all([live, legacy])
        await db.flush()
        live_owner = await create_lease(db, live.id, seconds=60)
        await db.commit()
        live_id, legacy_id = live.id, legacy.id

    occupied, release = asyncio.Event(), asyncio.Event()

    async def blocked(payload):
        occupied.set()
        await release.wait()

    worker = MemoryQueue(max_concurrent=1)
    worker.register("synthetic-blocker", blocked)
    monkeypatch.setattr(task_queue, "task_queue", worker)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    active_schedulers, completions, events = [], [], []
    await worker.start()
    try:
        await worker.enqueue("synthetic-blocker", "synthetic-blocker", {})
        await asyncio.wait_for(occupied.wait(), 5)
        for _ in range(scheduler_count):
            await scheduler.init_scheduler()
            active = scheduler.get_scheduler()
            active_schedulers.append(active)
            job = active.get_job("agent_run_reconcile")
            assert job.func is reap_expired_runs
            assert job.trigger.interval.total_seconds() == 30
            assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time == 30
            for other in active.get_jobs():
                active.pause_job(other.id)
            completed = asyncio.Event()
            completions.append(completed)

            def record(event, done=completed):
                if event.job_id == "agent_run_reconcile":
                    events.append(event)
                    done.set()

            active.add_listener(record, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        # Wait on the database's actual clock; no lease timestamp is forged.
        async with asyncio.timeout(5):
            while True:
                async with factory() as db:
                    expired = await db.scalar(select(AgentRunLease.id).where(
                        AgentRunLease.run_id == owner.run_id,
                        AgentRunLease.expires_at_epoch <= _clock(db),
                    ))
                if expired:
                    break
                await asyncio.sleep(0.02)
        for active in active_schedulers:
            active.modify_job("agent_run_reconcile", next_run_time=utcnow())
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in completions)), 10)
        assert all(event.exception is None for event in events)
        assert [run for event in events for run in event.retval] == [owner.run_id]
        assert (await worker.status("synthetic-blocker"))["status"] == "running"
        async with factory() as db:
            retired = await db.get(AgentRun, owner.run_id)
            assert retired.status == "failed" and retired.finished_at is not None
            assert retired.total_resources == retired.matched == 1
            assert (await db.get(Agent, agent_id)).last_run_status == "failed"
            assert (await db.get(AgentRun, live_id)).status == "running"
            assert (await db.get(AgentRun, legacy_id)).status == "running"
            assert list(await db.scalars(select(AgentRunLease.token))) == [live_owner.token]
    finally:
        for active in active_schedulers:
            active.shutdown(wait=False)
        await asyncio.sleep(0)
        scheduler._scheduler = None
        release.set()
        await worker.stop()
        if worker._run_tasks:
            await asyncio.gather(*worker._run_tasks, return_exceptions=True)
