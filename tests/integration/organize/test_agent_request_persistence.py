"""Real transaction/constraint checks; synthetic resource identity only."""
import uuid

from sqlalchemy import delete, select

from app.models.agent import Agent
from app.models.agent_resource_request import AgentResourceRequest
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.agent_resource_requests import acknowledge_requests, request_resources, snapshot_requests
from tests.integration.organize.test_organize_pipeline import _seed_chain


async def test_requests_are_atomic_versioned_and_cascade(db_session, session_factory, tmp_path):
    collection = WorkCollection(id=str(uuid.uuid4()), title_cn="synthetic request")
    db_session.add(collection)
    series = TVSeries(id=str(uuid.uuid4()), title_cn="synthetic request", collection_id=collection.id, season_number=1)
    chain = await _seed_chain(db_session, work=series, download_dir=str(tmp_path), resource_kw={"season": 1, "episode": 1})
    await db_session.commit()
    agent_id, resource_id = chain.agent.id, chain.resource.id
    async with session_factory() as writer:
        await request_resources(writer, [agent_id], [resource_id])
        # Closing without commit simulates a failed resource save.
    async with session_factory() as observer:
        assert await snapshot_requests(observer, agent_id) == []
    async with session_factory() as writer:
        await request_resources(writer, [agent_id, agent_id], [resource_id, resource_id])
        await writer.commit()
        [first] = await snapshot_requests(writer, agent_id)
        assert first.revision == 1
    async with session_factory() as writer:
        await request_resources(writer, [agent_id], [resource_id])
        await writer.commit()
        [second] = await snapshot_requests(writer, agent_id)
        assert second.id == first.id and second.revision == 2
        await acknowledge_requests(writer, [first])
        await writer.commit()
        assert await snapshot_requests(writer, agent_id) == [second]
        await acknowledge_requests(writer, [second])
        await writer.commit()
        await request_resources(writer, [agent_id], [resource_id])
        await writer.commit()
        [third] = await snapshot_requests(writer, agent_id)
        assert third.id != second.id and third.revision == 1
        await acknowledge_requests(writer, [first, second])
        await writer.commit()
        assert await snapshot_requests(writer, agent_id) == [third]
        await writer.execute(delete(Agent).where(Agent.id == agent_id))
        await writer.commit()
    async with session_factory() as observer:
        assert await observer.scalar(select(AgentResourceRequest.id)) is None


async def test_backoff_cap_and_new_revision_bypasses_old_failure(
    db_session, session_factory, tmp_path, monkeypatch,
):
    from datetime import datetime, timedelta

    from app.services import agent_resource_requests as requests
    from tests.integration.organize.test_agent_request_failures import seed

    chain = await seed(db_session, tmp_path)
    now = datetime(2026, 9, 13)
    monkeypatch.setattr(requests, "utcnow", lambda: now)
    async with session_factory() as writer:
        await requests.request_resources(writer, [chain.agent.id], [chain.resource.id])
        await writer.commit()
        [original] = await requests.snapshot_requests(writer, chain.agent.id)
        for attempt in range(1, 10):
            await requests.defer_requests(writer, [original], "synthetic processing failure")
            await writer.commit()
            row = await writer.get(AgentResourceRequest, original.id, populate_existing=True)
            assert row.attempt_count == attempt
            assert row.next_attempt_at == now + timedelta(seconds=min(1800, 30 * 2 ** (attempt - 1)))
            assert await requests.snapshot_requests(writer, chain.agent.id) == []
        await requests.request_resources(writer, [chain.agent.id], [chain.resource.id])
        await writer.commit()
        [edited] = await requests.snapshot_requests(writer, chain.agent.id)
        assert edited.revision == original.revision + 1
        await requests.defer_requests(writer, [original], "late stale failure")
        await writer.commit()
        row = await writer.get(AgentResourceRequest, original.id, populate_existing=True)
        assert row.attempt_count == 0 and row.next_attempt_at is None and row.error_message is None
        assert await requests.snapshot_requests(writer, chain.agent.id) == [edited]
