"""Real scheduler recovery and real sibling healing; synthetic episode metadata."""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.job_handlers import _handle_run_agent
from app.models.agent_resource_request import AgentResourceRequest
from app.models.agent_run import AgentRun
from app.models.file_resource import FileResource
from app.services import agent_resource_requests as requests
from app.services import scheduler
from app.services import task_queue as queue_module
from app.services.task_queue import MemoryQueue
from app.utils.time import utcnow
from tests.api.conftest import _build_test_app
from tests.integration.organize.test_agent_request_failures import seed


async def test_real_scheduler_recovers_persisted_request(db_session, session_factory, tmp_path, monkeypatch):
    chain = await seed(db_session, tmp_path)
    await requests.request_resources(db_session, [chain.agent.id], [chain.resource.id])
    await db_session.commit()
    worker = MemoryQueue()
    worker.register("run_agent", _handle_run_agent)
    monkeypatch.setattr(queue_module, "task_queue", worker)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    await worker.start()
    try:
        await scheduler.init_scheduler()
        active = scheduler.get_scheduler()
        job = active.get_job("agent_resource_dispatch")
        assert job is not None and job.func is requests.dispatch_pending_requests
        assert job.trigger.interval.total_seconds() == 5
        assert job.coalesce and job.max_instances == 1
        # Advance only this registered job; APScheduler invokes the actual callback.
        for other in active.get_jobs():
            if other.id != job.id:
                active.pause_job(other.id)
        active.modify_job(job.id, next_run_time=utcnow())
        async with asyncio.timeout(10):
            while True:
                async with session_factory() as observer:
                    run = await observer.scalar(select(AgentRun).where(AgentRun.status == "success"))
                    if run:
                        assert run.total_resources == 1
                        assert await observer.scalar(select(AgentResourceRequest.id)) is None
                        break
                await asyncio.sleep(0.02)
    finally:
        await scheduler.shutdown_scheduler()
        await worker.stop()
        if worker._run_tasks:
            await asyncio.gather(*worker._run_tasks, return_exceptions=True)


@pytest.mark.parametrize(
    "suffix,body",
    [
        ("/episode", {"episode": 6, "season": 1, "absolute_episode": 30}),
        ("", {"episode": 6, "season": 1, "absolute_episode": 30}),
    ],
)
async def test_healed_sibling_is_persisted_with_edit_during_queue_outage(
    db_session,
    session_factory,
    tmp_path,
    monkeypatch,
    suffix,
    body,
):
    chain = await seed(db_session, tmp_path)
    chain.resource.series_id = chain.work.id
    chain.resource.episode = 30
    chain.resource.absolute_episode = 30
    chain.resource.season = 1
    chain.resource.episode_confidence = "ambiguous"
    chain.resource.subtitle_group = "synthetic-group"
    sibling = FileResource(
        id=str(uuid.uuid4()),
        channel_id=chain.channel.id,
        series_id=chain.work.id,
        guid=str(uuid.uuid4()),
        title_raw="synthetic absolute episode 31",
        torrent_url="magnet:?xt=urn:btih:synthetic",
        season=1,
        episode=31,
        absolute_episode=31,
        episode_confidence="ambiguous",
        subtitle_group="synthetic-group",
    )
    db_session.add(sibling)
    await db_session.commit()

    class UnavailableQueue:
        async def enqueue(self, *args, **kwargs):
            raise ConnectionError("explicit broker outage")

    monkeypatch.setattr(queue_module, "task_queue", UnavailableQueue())
    app = _build_test_app(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(f"/api/v1/resources/{chain.resource.id}{suffix}", json=body)
        assert response.status_code == 200, response.text
    async with session_factory() as observer:
        healed = await observer.get(FileResource, sibling.id)
        assert (healed.season, healed.episode, healed.episode_confidence) == (1, 7, "reconciled")
        pending = await requests.snapshot_requests(observer, chain.agent.id)
        assert {row.resource_id for row in pending} == {chain.resource.id, sibling.id}
