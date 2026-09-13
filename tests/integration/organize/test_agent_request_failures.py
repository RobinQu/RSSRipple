"""Actual API/queue/database failure boundaries; synthetic unlinked metadata."""
import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.job_handlers import _handle_run_agent
from app.models.agent_resource_request import AgentResourceRequest
from app.models.agent_run import AgentRun
from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import agent_resource_requests as requests
from app.services import task_queue as queue_module
from app.services.task_queue import MemoryQueue
from tests.api.conftest import _build_test_app
from tests.integration.organize.test_organize_pipeline import _seed_chain


async def seed(db, tmp_path):
    collection = WorkCollection(id=str(uuid.uuid4()), title_cn="synthetic request failure")
    db.add(collection)
    series = TVSeries(id=str(uuid.uuid4()), title_cn="synthetic request failure",
                      collection_id=collection.id, season_number=1)
    chain = await _seed_chain(db, work=series, download_dir=str(tmp_path), resource_kw={})
    chain.resource.series_id = None
    await db.commit()
    return chain


@pytest.mark.parametrize("method,suffix,body", [
    ("PATCH", "", {"resolution": "2160p"}),
    ("PATCH", "/episode", {"episode": 10}),
    ("PUT", "/associations", {"is_batch": False, "works": [], "fields": {"resolution": "2160p"}}),
])
async def test_request_write_failure_rolls_back_resource_edit(
    db_session, session_factory, tmp_path, monkeypatch, method, suffix, body,
):
    chain = await seed(db_session, tmp_path)
    before = (chain.resource.resolution, chain.resource.episode)
    real_record = requests.request_channel_resources

    async def failed_record(*args):
        await real_record(*args)
        raise RuntimeError("injected failure after real request INSERT")

    monkeypatch.setattr("app.api.v1.resources.request_channel_resources", failed_record)
    app = _build_test_app(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://test") as client:
        response = await client.request(method, f"/api/v1/resources/{chain.resource.id}{suffix}", json=body)
        assert response.status_code == 500, response.text
    async with session_factory() as observer:
        row = await observer.get(FileResource, chain.resource.id)
        assert (row.resolution, row.episode) == before
        assert await observer.scalar(select(AgentResourceRequest.id)) is None


async def test_queue_outage_keeps_request_for_replacement_worker(db_session, session_factory, tmp_path, monkeypatch):
    chain = await seed(db_session, tmp_path)

    class UnavailableQueue:
        async def enqueue(self, *args, **kwargs):
            raise ConnectionError("explicit broker outage")

    monkeypatch.setattr(queue_module, "task_queue", UnavailableQueue())
    app = _build_test_app(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(f"/api/v1/resources/{chain.resource.id}", json={"resolution": "2160p"})
        assert response.status_code == 200, response.text
    async with session_factory() as observer:
        [pending] = await requests.snapshot_requests(observer, chain.agent.id)
        assert pending.resource_id == chain.resource.id
    worker = MemoryQueue()
    worker.register("run_agent", _handle_run_agent)
    monkeypatch.setattr(queue_module, "task_queue", worker)
    await worker.start()
    try:
        await requests.dispatch_pending_requests()
        async with asyncio.timeout(10):
            await worker._queue.join()
        async with session_factory() as observer:
            assert await requests.snapshot_requests(observer, chain.agent.id) == []
            [run] = (await observer.scalars(select(AgentRun))).all()
            assert run.total_resources == 1 and run.status == "success"
    finally:
        await worker.stop()
        if worker._run_tasks:
            await asyncio.gather(*worker._run_tasks, return_exceptions=True)


@pytest.mark.parametrize("failure_mode", ["result", "exception"])
async def test_failed_job_defers_only_its_requests_and_recovers_when_due(
    db_session, session_factory, tmp_path, monkeypatch, failure_mode,
):
    from datetime import timedelta

    from app.services import agent_service
    from app.utils.time import utcnow

    bad = await seed(db_session, tmp_path / "bad")
    good = await seed(db_session, tmp_path / "good")
    now = utcnow()
    monkeypatch.setattr(requests, "utcnow", lambda: now)
    async with session_factory() as writer:
        await requests.request_resources(writer, [bad.agent.id], [bad.resource.id])
        await requests.request_resources(writer, [good.agent.id], [good.resource.id])
        await writer.commit()
    real_process = agent_service.process_resources
    fail = True

    async def process(agent, resources, *args, **kwargs):
        if agent.id == bad.agent.id and fail:
            if failure_mode == "exception":
                raise RuntimeError("controlled job failure")
            return agent_service.RunResult(total_resources=len(resources), errors=["controlled job failure"])
        return await real_process(agent, resources, *args, **kwargs)

    monkeypatch.setattr(agent_service, "process_resources", process)
    worker = MemoryQueue()
    worker.register("run_agent", _handle_run_agent)
    monkeypatch.setattr(queue_module, "task_queue", worker)
    await worker.start()
    try:
        await requests.dispatch_pending_requests()
        async with asyncio.timeout(10):
            await worker._queue.join()
        async with session_factory() as observer:
            [pending] = (await observer.scalars(select(AgentResourceRequest))).all()
            assert pending.agent_id == bad.agent.id and pending.attempt_count == 1
            assert pending.next_attempt_at == now + timedelta(seconds=30)
            assert "controlled job failure" in pending.error_message
            [good_run] = (await observer.scalars(select(AgentRun).where(AgentRun.agent_id == good.agent.id))).all()
            assert good_run.status == "success"
            assert len((await observer.scalars(select(AgentRun))).all()) == 2
        await requests.dispatch_pending_requests()
        async with asyncio.timeout(10):
            await worker._queue.join()
        async with session_factory() as observer:
            assert len((await observer.scalars(select(AgentRun))).all()) == 2
        now += timedelta(seconds=31)
        fail = False
        await requests.dispatch_pending_requests()
        async with asyncio.timeout(10):
            await worker._queue.join()
        async with session_factory() as observer:
            assert await observer.scalar(select(AgentResourceRequest.id)) is None
            runs = (await observer.scalars(select(AgentRun))).all()
            assert len(runs) == 3 and sum(run.status == "success" for run in runs) == 2
    finally:
        await worker.stop()
        if worker._run_tasks:
            await asyncio.gather(*worker._run_tasks, return_exceptions=True)


async def test_paused_agent_retains_request_and_channel_change_discards_stale_request(
    db_session, session_factory, tmp_path, monkeypatch,
):
    from app.models.agent import Agent

    chain = await seed(db_session, tmp_path / "original")
    other = await seed(db_session, tmp_path / "other")
    async with session_factory() as writer:
        await requests.request_resources(writer, [chain.agent.id], [chain.resource.id])
        agent = await writer.get(Agent, chain.agent.id)
        agent.status = "paused"
        await writer.commit()
    worker = MemoryQueue()
    worker.register("run_agent", _handle_run_agent)
    monkeypatch.setattr(queue_module, "task_queue", worker)
    await worker.start()
    try:
        await requests.dispatch_pending_requests()
        assert await worker.list_jobs() == []
        async with session_factory() as writer:
            assert await writer.scalar(select(AgentResourceRequest.id)) is not None
            agent = await writer.get(Agent, chain.agent.id)
            agent.status = "active"
            await writer.commit()
        await requests.dispatch_pending_requests()
        async with asyncio.timeout(10):
            await worker._queue.join()
        async with session_factory() as writer:
            assert await writer.scalar(select(AgentResourceRequest.id)) is None
            await requests.request_resources(writer, [chain.agent.id], [chain.resource.id])
            agent = await writer.get(Agent, chain.agent.id)
            agent.channel_id = other.agent.channel_id
            await writer.commit()
        await requests.dispatch_pending_requests()
        async with asyncio.timeout(10):
            await worker._queue.join()
        async with session_factory() as observer:
            assert await observer.scalar(select(AgentResourceRequest.id)) is None
            runs = (await observer.scalars(select(AgentRun))).all()
            assert len(runs) == 1 and runs[0].total_resources == 1
    finally:
        await worker.stop()
        if worker._run_tasks:
            await asyncio.gather(*worker._run_tasks, return_exceptions=True)
