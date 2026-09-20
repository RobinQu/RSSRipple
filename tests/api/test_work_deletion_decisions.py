"""Deletion must archive obsolete choices without erasing human history."""

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.decision_migration import DecisionMigration
from app.models.pending_decision import PendingDecision
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.services.agent_service import _batch_coverage_key, create_pending_decision
from app.services.resource_coverage import load_batch_coverage
from tests.unit.test_agent_service import _make_resource


@pytest.mark.parametrize("fail_after_rekey", [False, True])
@pytest.mark.parametrize("kind", ["series", "movie"])
@pytest.mark.parametrize("links_only", [False, True])
async def test_deleted_work_expires_choice_and_archives_original_identity(
    client, db_session_factory, sample_channel, sample_downloader, sample_movie, sample_series, kind, links_only, fail_after_rekey, monkeypatch
):
    work = sample_movie if kind == "movie" else sample_series
    async with db_session_factory() as db:
        agent = Agent(
            name="Synthetic deletion choice", channel_id=sample_channel.id,
            downloader_id=sample_downloader.id, scope_channel_wide=True,
        )
        season = work.season_number if kind == "series" else None
        episode = 1 if kind == "series" else None
        resources = [
            _make_resource(sample_channel.id, season=season, episode=episode,
                           **{kind + "_id": None if links_only else work.id})
            for _ in range(2)
        ]
        db.add_all([agent, *resources])
        await db.flush()
        if links_only:
            for resource in resources:
                resource.is_batch = True
                resource.batch_scope = "season" if kind == "series" else "movies"
                db.add(ResourceWorkLink(resource_id=resource.id, source="auto", **{kind + "_id": work.id}))
                db.add(ResourceFileAssignment(
                    resource_id=resource.id, file_path="synthetic.mkv", source="auto",
                    season=season, episode_start=episode, episode_end=episode, **{kind + "_id": work.id},
                ))
            await db.flush()
            await load_batch_coverage(db, resources)
            key, coverage = (kind, None, None, -1), _batch_coverage_key(resources[0])
            assert coverage is not None
        else:
            key, coverage = (kind, work.id, episode), None
        await create_pending_decision(agent, key, resources, db, skip_llm=True, coverage=coverage)
        row = await db.scalar(select(PendingDecision).where(PendingDecision.agent_id == agent.id))
        decision_id, candidates, scope = row.id, list(row.candidates), dict(row.decision_scope)
        history = PendingDecision(
            agent_id=agent.id, status="decided", candidates=candidates,
            decided_resource_id=candidates[0], reason="Synthetic retained human history",
            **{kind + "_id": work.id},
        )
        db.add(history)
        await db.commit()
        history_id = history.id
    if fail_after_rekey:
        import httpx

        import app.services.decision_rekey as rekey
        from tests.api.conftest import _build_test_app

        real_rekey = rekey.rekey_agent_choices

        async def fail_after_archive(*args):
            await real_rekey(*args)
            raise RuntimeError("Synthetic failure after deletion decision archive")

        monkeypatch.setattr(rekey, "rekey_agent_choices", fail_after_archive)
        route = "movies" if kind == "movie" else "series"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_build_test_app(db_session_factory), raise_app_exceptions=False),
            base_url="http://test",
        ) as api:
            response = await api.delete(f"/api/v1/{route}/{work.id}")
        assert response.status_code == 500
        async with db_session_factory() as db:
            assert await db.get(type(work), work.id) is not None
            row = await db.get(PendingDecision, decision_id)
            assert row.status == "pending" and row.candidates == candidates and row.decision_scope == scope
            assert not list(await db.scalars(select(DecisionMigration)))
            history = await db.get(PendingDecision, history_id)
            assert history.status == "decided" and history.decided_resource_id == candidates[0]
            if links_only:
                assignments = list(await db.scalars(select(ResourceFileAssignment)))
                assert len(assignments) == 2
                assert all(getattr(row, kind + "_id") == work.id for row in assignments)
        return
    route = "movies" if kind == "movie" else "series"
    response = await client.delete(f"/api/v1/{route}/{work.id}")
    assert response.status_code == 200, response.text
    async with db_session_factory() as db:
        row = await db.get(PendingDecision, decision_id)
        assert row.status == "expired"
        assert row.candidates == candidates
        assert row.decision_scope == scope
        archives = list(await db.scalars(select(DecisionMigration)))
        assert any(decision_id in archive.result.get("superseded_ids", []) for archive in archives)
        archive = next(archive for archive in archives if decision_id in archive.result.get("superseded_ids", []))
        original = next(row for row in archive.original_review["original_decisions"] if row["id"] == decision_id)
        assert original[kind + "_id"] == (None if links_only else work.id)
        assert original["status"] == "pending"
        assert original["candidates"] == candidates
        assert original["decision_scope"] == scope
        history = await db.get(PendingDecision, history_id)
        assert history.status == "decided"
        assert history.reason == "Synthetic retained human history"
        assert history.candidates == candidates
        assert history.decided_resource_id == candidates[0]
