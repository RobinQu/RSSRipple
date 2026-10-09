"""Real router + Turso persistence probes for the documented work limit.

All identities/counts are synthetic; a torrent corpus is not relevant here.
"""

import httpx
import pytest
from sqlalchemy import func, select

from app.models.agent import Agent
from app.models.agent_work import AgentWork
from app.models.movie import Movie
from tests.api.test_agents import channel_and_dl  # noqa: F401


@pytest.mark.parametrize("scenario", ["replace-eleven", "narrow-existing-eleven", "replace-ten", "wide-eleven", "narrow-with-ten", "narrow-with-empty", "wide-null"])
async def test_effective_work_limit(client, channel_and_dl, db_session, scenario, record_property):  # noqa: F811
    channel_id, downloader_id = channel_and_dl
    movies = [Movie(title_cn=f"Synthetic cap probe {i}") for i in range(11)]
    db_session.add_all(movies)
    await db_session.commit()
    works = [{"movie_id": m.id, "content_type": "movie"} for m in movies]
    initially_wide = scenario in {"narrow-existing-eleven", "narrow-with-ten", "narrow-with-empty", "wide-null"}
    original = works if initially_wide else works[:1]
    response = await client.post("/api/v1/agents", json={
        "name": "Synthetic cap probe", "channel_id": channel_id, "downloader_id": downloader_id,
        "scope_channel_wide": initially_wide, "works": original,
    })
    assert response.status_code == 201
    agent_id = response.json()["data"]["id"]
    body = {"name": "Changed name"}
    if initially_wide:
        body["scope_channel_wide"] = scenario == "wide-null"
        if scenario == "narrow-with-ten":
            body["works"] = works[:10]
        elif scenario == "narrow-with-empty":
            body["works"] = []
        elif scenario == "wide-null":
            body["works"] = None
    else:
        body["works"] = works[:10] if scenario == "replace-ten" else works
        if scenario == "wide-eleven":
            body["scope_channel_wide"] = True
    response = await client.put(f"/api/v1/agents/{agent_id}", json=body)
    db_session.expire_all()
    count = await db_session.scalar(select(func.count()).select_from(AgentWork).where(AgentWork.agent_id == agent_id))
    agent = await db_session.get(Agent, agent_id)
    record_property("observed_status", response.status_code)
    record_property("persisted_works", count)
    record_property("persisted_scope_channel_wide", agent.scope_channel_wide)
    if scenario in {"replace-eleven", "narrow-existing-eleven"}:
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        assert count == len(original)
        assert agent.name == "Synthetic cap probe"
        assert agent.scope_channel_wide == initially_wide
    else:
        assert response.status_code == 200
        assert count == (0 if scenario == "narrow-with-empty" else 10 if scenario in {"replace-ten", "narrow-with-ten"} else 11)


@pytest.mark.parametrize("first_edit", ["add", "replace", "narrow"])
async def test_concurrent_edits_cannot_exceed_limit(client, channel_and_dl, db_session, monkeypatch, record_property, first_edit):  # noqa: F811
    """A real Turso write-write conflict between two overlapping edits must
    still leave the limit intact. The retry middleware no longer replays
    POST/PUT (out-of-band side effects cannot be undone), so the losing
    request's conflict propagates to the client (a 500 in production; the
    ASGI test transport re-raises it) and its transaction rolls back; the
    client may retry, at which point the limit check answers 400."""
    import asyncio

    from sqlalchemy.exc import DatabaseError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.v1 import agents as agents_module
    from app.database import _is_retryable_lock_error

    channel_id, downloader_id = channel_and_dl
    movies = [Movie(title_cn=f"Synthetic concurrent cap {i}") for i in range(11)]
    db_session.add_all(movies)
    await db_session.commit()
    works = [{"movie_id": m.id, "content_type": "movie"} for m in movies]
    response = await client.post("/api/v1/agents", json={
        "name": "Concurrent cap", "channel_id": channel_id, "downloader_id": downloader_id,
        "works": works[:10 if first_edit == "narrow" else 9], "scope_channel_wide": first_edit == "narrow",
    })
    assert response.status_code == 201
    agent_id = response.json()["data"]["id"]
    original_execute = AsyncSession.execute
    original_lock = agents_module._lock_agent_rules
    reached = asyncio.Event()
    conflict_seen = asyncio.Event()
    held = False
    real_conflicts = 0

    async def pause_first_writer(db, identity):
        nonlocal held
        await original_lock(db, identity)
        if not held:
            held = True
            reached.set()
            await asyncio.wait_for(conflict_seen.wait(), 5)

    async def observe_real_conflict(session, *args, **kwargs):
        nonlocal real_conflicts
        try:
            return await original_execute(session, *args, **kwargs)
        except DatabaseError as exc:
            assert _is_retryable_lock_error(exc)
            real_conflicts += 1
            conflict_seen.set()
            raise

    monkeypatch.setattr(AsyncSession, "execute", observe_real_conflict)
    monkeypatch.setattr(agents_module, "_lock_agent_rules", pause_first_writer)
    first = asyncio.create_task(
        client.post(f"/api/v1/agents/{agent_id}/works", json=works[9]) if first_edit == "add" else
        client.put(f"/api/v1/agents/{agent_id}", json={"works": works[:10]} if first_edit == "replace" else {"scope_channel_wide": False})
    )
    second = None
    try:
        await asyncio.wait_for(reached.wait(), 5)
        second = asyncio.create_task(client.post(f"/api/v1/agents/{agent_id}/works", json=works[10]))
        responses = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 10)
        count = await db_session.scalar(select(func.count()).select_from(AgentWork).where(AgentWork.agent_id == agent_id))
        record_property("real_database_conflicts", real_conflicts)
        record_property("statuses", str([
            r.status_code if isinstance(r, httpx.Response) else type(r).__name__ for r in responses
        ]))
        record_property("persisted_works", count)
        assert real_conflicts >= 1
        assert count == 10
        first_reply, second_reply = responses
        assert first_reply.status_code == (201 if first_edit == "add" else 200)
        # The conflicting request is NOT replayed (POST/PUT are excluded from
        # the retry middleware); the write-write conflict reaches the client.
        assert isinstance(second_reply, DatabaseError)
        assert _is_retryable_lock_error(second_reply)
        assert not (await db_session.get(Agent, agent_id)).scope_channel_wide
    finally:
        conflict_seen.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*[t for t in (first, second) if t], return_exceptions=True)


@pytest.mark.parametrize("selected", [[], ["synthetic-selected-resource"]])
async def test_rejected_update_preserves_progress_and_all_fields(client, channel_and_dl, db_session, monkeypatch, selected):  # noqa: F811
    from datetime import datetime
    from unittest.mock import AsyncMock

    from app.models.agent_publication_progress import AgentPublicationProgress
    from app.models.channel import Channel
    from app.models.download_task import DownloadTask
    from app.services import task_queue

    channel_id, downloader_id = channel_and_dl
    movies = [Movie(title_cn=f"Synthetic rejected edit {i}") for i in range(11)]
    other = Channel(name="Synthetic alternate channel", type="rss_feed", url="https://example.invalid/other", field_mapping={})
    db_session.add_all([*movies, other])
    await db_session.commit()
    works = [{"movie_id": m.id, "content_type": "movie"} for m in movies]
    response = await client.post("/api/v1/agents", json={
        "name": "Original name", "channel_id": channel_id, "downloader_id": downloader_id,
        "works": works[:1], "scope_channel_wide": False,
    })
    assert response.status_code == 201
    agent_id = response.json()["data"]["id"]
    agent = await db_session.get(Agent, agent_id)
    watermark = datetime(2024, 1, 2, 3, 4, 5)
    agent.last_consumed_at = watermark
    db_session.add(AgentPublicationProgress(agent_id=agent_id, channel_id=channel_id,
        generation="synthetic-generation", baseline=5, cursor=7))
    await db_session.commit()
    original_ids = list(await db_session.scalars(select(AgentWork.id).where(AgentWork.agent_id == agent_id)))
    await db_session.refresh(agent, ["updated_at"])
    original_updated_at = agent.updated_at
    backfill = AsyncMock(side_effect=AssertionError("Rejected update must not enter backfill"))
    monkeypatch.setattr("app.api.v1.agents._apply_backfill", backfill)
    task_queue.task_queue.enqueue.reset_mock()
    response = await client.put(f"/api/v1/agents/{agent_id}", json={
        "name": "Changed", "channel_id": other.id, "status": "paused",
        "works": works, "dispatch_resource_ids": selected,
    })
    assert response.status_code == 422
    db_session.expire_all()
    agent = await db_session.get(Agent, agent_id)
    progress = await db_session.scalar(select(AgentPublicationProgress).where(AgentPublicationProgress.agent_id == agent_id))
    assert (agent.name, agent.channel_id, agent.status, agent.last_consumed_at, agent.updated_at) == (
        "Original name", channel_id, "active", watermark, original_updated_at,
    )
    assert (progress.channel_id, progress.generation, progress.baseline, progress.cursor) == (
        channel_id, "synthetic-generation", 5, 7,
    )
    assert list(await db_session.scalars(select(AgentWork.id).where(AgentWork.agent_id == agent_id))) == original_ids
    assert await db_session.scalar(select(func.count()).select_from(DownloadTask)) == 0
    backfill.assert_not_awaited()
    task_queue.task_queue.enqueue.assert_not_awaited()
