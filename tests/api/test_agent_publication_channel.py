"""Actual update API preserves null-save scope on a channel switch."""
from datetime import timedelta

from app.models.agent import Agent
from app.services.agent_publication_progress import snapshot_publications
from app.services.resource_publication import publish_resource
from app.utils.time import utcnow
from tests.unit.test_agent_publication_progress import next_resource, setup
from tests.unit.test_resource_publication import resource


async def test_channel_update_preserves_timestamp_but_changes_publication_scope(client, db_session_factory):
    async with db_session_factory() as db:
        agent, _ = await setup(db)
        old_snapshot = await snapshot_publications(db, agent.id, agent.channel_id)
        old = await resource(db)
        now = utcnow()
        old.created_at = now - timedelta(days=2)
        await publish_resource(db, old.id, kind="created")
        new = await next_resource(db, old.channel_id)
        new.created_at = now
        agent.last_consumed_at = now - timedelta(days=1)
        await db.commit()
        aid, cid, rid, watermark = agent.id, old.channel_id, new.id, agent.last_consumed_at
    response = await client.put(f"/api/v1/agents/{aid}", json={"channel_id": cid})
    assert response.status_code == 200, response.text
    async with db_session_factory() as db:
        updated = await db.get(Agent, aid)
        assert updated.last_consumed_at == watermark
        snapshot = await snapshot_publications(db, aid, cid)
        assert snapshot.resource_ids == (rid,)
        assert snapshot.generation != old_snapshot.generation
    response = await client.put(f"/api/v1/agents/{aid}", json={"name": "Renamed"})
    assert response.status_code == 200, response.text
    async with db_session_factory() as db:
        assert await snapshot_publications(db, aid, cid) == snapshot
    response = await client.put(f"/api/v1/agents/{aid}", json={"dispatch_resource_ids": []})
    assert response.status_code == 200, response.text
    async with db_session_factory() as db:
        assert not (await snapshot_publications(db, aid, cid)).resource_ids


async def test_rule_and_subscription_edits_invalidate_runs_without_resetting_progress(client, db_session_factory):
    from sqlalchemy import select

    from app.models.agent_publication_progress import AgentPublicationProgress
    from app.models.movie import Movie

    async with db_session_factory() as db:
        agent, _ = await setup(db)
        resource = await next_resource(db, agent.channel_id)
        movie = Movie(title_cn="Synthetic subscription")
        db.add(movie)
        agent.scope_channel_wide = True
        agent.last_consumed_at = utcnow()
        await db.commit()
        aid, cid, rid, mid = agent.id, agent.channel_id, resource.id, movie.id

    async def state():
        async with db_session_factory() as db:
            row = await db.scalar(select(AgentPublicationProgress).where(AgentPublicationProgress.agent_id == aid))
            agent = await db.get(Agent, aid)
            snapshot = await snapshot_publications(db, aid, cid)
            assert snapshot.resource_ids == (rid,)
            return row.generation, (row.baseline, row.cursor, row.historical_created_after, agent.last_consumed_at)

    generation, boundaries = await state()
    response = await client.put(f"/api/v1/agents/{aid}", json={"scope_channel_wide": False})
    assert response.status_code == 200, response.text
    fresh, current_boundaries = await state()
    assert fresh != generation and current_boundaries == boundaries
    generation = fresh
    response = await client.post(f"/api/v1/agents/{aid}/works", json={"content_type": "movie", "movie_id": mid})
    assert response.status_code == 201, response.text
    wid = response.json()["data"]["id"]
    fresh, current_boundaries = await state()
    assert fresh != generation and current_boundaries == boundaries
    generation = fresh
    response = await client.put(f"/api/v1/agents/{aid}/works/{wid}", json={"enable_episode_dedup": False})
    assert response.status_code == 200, response.text
    fresh, current_boundaries = await state()
    assert fresh != generation and current_boundaries == boundaries
    generation = fresh
    response = await client.delete(f"/api/v1/agents/{aid}/works/{wid}")
    assert response.status_code == 200, response.text
    fresh, current_boundaries = await state()
    assert fresh != generation and current_boundaries == boundaries
