"""Queue failures cannot acknowledge database-owned publication work."""

from unittest.mock import AsyncMock

from app.models.agent import Agent
from app.services import task_queue
from app.services.agent_publication_progress import acknowledge_publications, snapshot_publications
from app.services.publication_dispatch import dispatch_pending_publications
from tests.unit.test_agent_publication_progress import next_resource, setup


async def test_failed_wakeup_retries_and_paused_agent_retains_work(db_session, monkeypatch):
    agent, _ = await setup(db_session)
    resource = await next_resource(db_session, agent.channel_id)
    agent.status = "paused"
    await db_session.commit()
    enqueue = AsyncMock(side_effect=RuntimeError("Synthetic queue outage"))
    monkeypatch.setattr(task_queue.task_queue, "enqueue", enqueue)
    await dispatch_pending_publications()
    enqueue.assert_not_called()
    agent.status = "active"
    await db_session.commit()
    await dispatch_pending_publications()
    enqueue.assert_awaited_once_with("run_agent", f"agent:{agent.id}", {"agent_id": agent.id, "automatic": True})
    pending = await snapshot_publications(db_session, agent.id, agent.channel_id)
    assert pending.resource_ids == (resource.id,)
    enqueue.side_effect = None
    await dispatch_pending_publications()
    assert enqueue.await_count == 2
    assert await snapshot_publications(db_session, agent.id, agent.channel_id) == pending
    await acknowledge_publications(db_session, pending)
    await db_session.commit()
    await dispatch_pending_publications()
    assert enqueue.await_count == 2


async def test_new_agent_without_progress_gets_initialization_wakeup(db_session, monkeypatch):
    agent, _ = await setup(db_session)
    fresh = Agent(name="Synthetic fresh", channel_id=agent.channel_id, downloader_id=agent.downloader_id)
    db_session.add(fresh)
    await db_session.commit()
    enqueue = AsyncMock()
    monkeypatch.setattr(task_queue.task_queue, "enqueue", enqueue)
    await dispatch_pending_publications()
    enqueue.assert_awaited_once_with("run_agent", f"agent:{fresh.id}", {"agent_id": fresh.id, "automatic": True})
