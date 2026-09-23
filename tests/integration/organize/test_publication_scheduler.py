"""Real five-second timer and MemoryQueue consuming synthetic DB publications."""

import asyncio

from apscheduler.events import EVENT_JOB_EXECUTED
from sqlalchemy import select

from app.config import settings
from app.job_handlers import _handle_run_agent
from app.models.agent_run import AgentRun
from app.services import scheduler, task_queue
from app.services.agent_publication_progress import snapshot_publications
from app.services.task_queue import MemoryQueue
from tests.unit.test_agent_publication_progress import next_resource, setup


async def test_real_timer_preserves_paused_progress_and_consumes_after_resume(db_session, monkeypatch):
    agent, _ = await setup(db_session)
    resource = await next_resource(db_session, agent.channel_id)
    agent.status = "paused"
    await db_session.commit()
    queue = MemoryQueue()
    queue.register("run_agent", _handle_run_agent)
    monkeypatch.setattr(task_queue, "task_queue", queue)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    tick = asyncio.Event()
    await queue.start()
    try:
        await scheduler.init_scheduler()
        sched = scheduler.get_scheduler()
        # Preserve the production timer unchanged; suppress unrelated jobs.
        for job in sched.get_jobs():
            if job.id != "agent_publication_dispatch":
                sched.remove_job(job.id)
        job = sched.get_job("agent_publication_dispatch")
        assert job.trigger.interval.total_seconds() == 5
        sched.add_listener(lambda event: tick.set(), EVENT_JOB_EXECUTED)
        await asyncio.wait_for(tick.wait(), timeout=20)
        assert await queue.status(f"agent:{agent.id}") is None
        assert (await snapshot_publications(db_session, agent.id, agent.channel_id)).resource_ids == (resource.id,)
        agent.status = "active"
        await db_session.commit()
        async with asyncio.timeout(20):
            while True:
                status = await queue.status(f"agent:{agent.id}")
                if status and status["status"] == "done":
                    break
                await asyncio.sleep(0.05)
        assert not (await snapshot_publications(db_session, agent.id, agent.channel_id)).resource_ids
        runs = list(await db_session.scalars(select(AgentRun).where(AgentRun.agent_id == agent.id)))
        assert len(runs) == 1
        assert runs[0].total_resources == 1
        assert runs[0].status == "success"
    finally:
        await scheduler.shutdown_scheduler()
        await asyncio.sleep(0)
        await queue.stop()
