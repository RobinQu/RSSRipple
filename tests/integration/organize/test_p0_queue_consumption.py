"""P0-2: router -> live queue -> production run handler -> committed AgentRun.

The raw title is captured input. This case deliberately leaves the work
unlinked, so the real confirmation gate rejects dispatch without external I/O.
It tests queue liveness/transaction visibility, not metadata reconstruction.
"""

import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.job_handlers import _handle_run_agent
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import task_queue as queue_module
from app.services.task_queue import MemoryQueue
from app.utils.time import utcnow
from tests.api.conftest import _build_test_app
from tests.integration.organize.test_organize_pipeline import _seed_chain
from tests.metadata_corpus.dataset import load_corpus


async def test_revision_uses_replaced_queue_and_consumes_old_resource(
    db_session, session_factory, tmp_path, monkeypatch,
):
    case = next(case for case in load_corpus()[1]["cases"]
                if case["id"] == "f79ef2eb-02d5-42d3-80dc-70dd3c1d733b")
    collection = WorkCollection(id=str(uuid.uuid4()), title_cn="猫与龙")
    db_session.add(collection)
    series = TVSeries(id=str(uuid.uuid4()), title_cn="猫与龙",
                      collection_id=collection.id, season_number=1)
    chain = await _seed_chain(
        db_session, work=series, download_dir=str(tmp_path),
        resource_kw={"title_raw": case["input"]["title_raw"]},
    )
    chain.resource.series_id = None  # controlled incomplete-metadata case
    chain.resource.created_at = utcnow() - timedelta(days=2)
    watermark = utcnow()
    chain.agent.last_consumed_at = watermark
    await db_session.flush()
    from app.services.publication_migration import bootstrap_publications

    await bootstrap_publications(db_session, writers_stopped=True)
    await db_session.commit()
    # Router already imported by _build_test_app BEFORE replacing the singleton.
    app = _build_test_app(session_factory)
    old_queue = queue_module.task_queue
    queue = MemoryQueue()
    queue.register("run_agent", _handle_run_agent)
    monkeypatch.setattr(queue_module, "task_queue", queue)
    await queue.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for resolution in ["1080p", "2160p"]:
                previous = await queue.status(f"agent:{chain.agent.id}")
                response = await client.patch(f"/api/v1/resources/{chain.resource.id}", json={"resolution": resolution})
                assert response.status_code == 200, response.text
                async with asyncio.timeout(10):
                    while True:
                        state = await queue.status(f"agent:{chain.agent.id}")
                        if state and state["status"] in {"done", "failed"} and (
                            previous is None or state["job_id"] != previous["job_id"]
                        ):
                            break
                        await asyncio.sleep(0.01)
                assert state["status"] == "done", state
                assert state["result"]["total_resources"] == 1
                assert state["result"]["unrecognized"] == 1
                assert not state["result"]["errors"]
                async with session_factory() as session:
                    resource = await session.get(FileResource, chain.resource.id)
                    assert resource.resolution == resolution
                    agent = await session.get(Agent, chain.agent.id)
                    assert agent.last_consumed_at == watermark  # targeted run never advances
            async with session_factory() as session:
                runs = (await session.execute(select(AgentRun).where(AgentRun.agent_id == chain.agent.id))).scalars().all()
                assert len(runs) == 2
                assert all(run.total_resources == 1 and run.status == "success" for run in runs)
        assert old_queue is not queue
    finally:
        await queue.stop()
        if queue._run_tasks:
            await asyncio.gather(*queue._run_tasks, return_exceptions=True)


@pytest.mark.parametrize("pending_before_start", [False, True])
@pytest.mark.parametrize("method,suffix,body,field,value", [
    ("PATCH", "", {"resolution": "2160p"}, "resolution", "2160p"),
    ("PATCH", "/episode", {"episode": 10}, "episode", 10),
    ("PUT", "/associations", {"is_batch": False, "works": [], "fields": {"resolution": "2160p"}},
     "resolution", "2160p"),
])
async def test_revision_while_agent_has_selected_resources_gets_followup_run(
    db_session, session_factory, tmp_path, monkeypatch, method, suffix, body, field, value, pending_before_start,
):
    from app.services import agent_service

    case = next(case for case in load_corpus()[1]["cases"]
                if case["id"] == "f79ef2eb-02d5-42d3-80dc-70dd3c1d733b")
    collection = WorkCollection(id=str(uuid.uuid4()), title_cn="猫与龙")
    db_session.add(collection)
    series = TVSeries(id=str(uuid.uuid4()), title_cn="猫与龙", collection_id=collection.id, season_number=1)
    chain = await _seed_chain(db_session, work=series, download_dir=str(tmp_path),
                              resource_kw={"title_raw": case["input"]["title_raw"]})
    chain.resource.series_id = None  # Controlled incomplete metadata; no RPC.
    chain.resource.created_at = utcnow() - timedelta(days=2)
    watermark = utcnow()
    chain.agent.last_consumed_at = watermark
    await db_session.flush()
    from app.services.publication_migration import bootstrap_publications

    await bootstrap_publications(db_session, writers_stopped=True)
    await db_session.commit()
    from app.services.agent_resource_requests import request_resources

    if pending_before_start:
        async with session_factory() as writer:
            await request_resources(writer, [chain.agent.id], [chain.resource.id])
            await writer.commit()
    selected, release = asyncio.Event(), asyncio.Event()
    real_process = agent_service.process_resources

    async def pause_first_run(*args, **kwargs):
        if not selected.is_set():
            selected.set()
            await release.wait()
        return await real_process(*args, **kwargs)

    monkeypatch.setattr(agent_service, "process_resources", pause_first_run)
    queue = MemoryQueue()
    queue.register("run_agent", _handle_run_agent)
    monkeypatch.setattr(queue_module, "task_queue", queue)
    app = _build_test_app(session_factory)
    await queue.start()
    try:
        await queue.enqueue("run_agent", f"agent:{chain.agent.id}", {"agent_id": chain.agent.id})
        async with asyncio.timeout(10):
            await selected.wait()  # Real job has committed selection and loaded its inputs.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.request(method, f"/api/v1/resources/{chain.resource.id}{suffix}", json=body)
            assert response.status_code == 200, response.text
        from app.services.agent_resource_requests import snapshot_requests

        async with session_factory() as observer:
            [pending] = await snapshot_requests(observer, chain.agent.id)
            assert pending.resource_id == chain.resource.id and pending.revision == (2 if pending_before_start else 1)
        release.set()
        async with asyncio.timeout(10):
            await queue._queue.join()
        from app.services.agent_resource_requests import dispatch_pending_requests

        async with session_factory() as observer:
            [remaining] = await snapshot_requests(observer, chain.agent.id)
            assert remaining == pending  # The first run cannot acknowledge the newer edit.
        await dispatch_pending_requests()  # Explicitly drive the periodic recovery tick.
        async with asyncio.timeout(10):
            await queue._queue.join()
        async with session_factory() as observer:
            resource = await observer.get(FileResource, chain.resource.id)
            assert getattr(resource, field) == value
            agent = await observer.get(Agent, chain.agent.id)
            assert agent.last_consumed_at == watermark
            runs = (await observer.scalars(select(AgentRun).where(AgentRun.agent_id == agent.id))).all()
            assert sorted(run.total_resources for run in runs) == ([1, 1] if pending_before_start else [0, 1])
            assert await snapshot_requests(observer, agent.id) == []
    finally:
        release.set()
        await queue.stop()
        if queue._run_tasks:
            await asyncio.gather(*queue._run_tasks, return_exceptions=True)
