"""Actual database comparison; review never turns ambiguous history into requests."""

import pytest
from sqlalchemy import select

from app.models.agent_publication_progress import AgentPublicationProgress
from app.services.agent_publication_progress import snapshot_publications
from scripts.review_publication_migration import apply_review, export_review
from tests.unit.test_publication_migration import legacy


async def test_review_preserves_pending_and_lists_excluded_history(db_session):
    agent, old, rows = await legacy(db_session)
    report = await export_review(db_session)
    item = report["agents"][0]
    assert item["pending_resource_ids"] == [rows[1].id]
    assert set(item["excluded_or_ambiguous_resource_ids"]) == {old.id, rows[0].id}
    assert await db_session.scalar(select(AgentPublicationProgress.id)) is None
    report["approved_fingerprint"] = report["fingerprint"]
    assert (await apply_review(db_session, report))["status"] == "applied"
    before = await snapshot_publications(db_session, agent.id, old.channel_id)
    assert before.resource_ids == (rows[1].id,)
    assert await apply_review(db_session, report) == {"status": "already_applied"}
    assert await snapshot_publications(db_session, agent.id, old.channel_id) == before


async def test_changed_watermark_requires_new_review(db_session):
    agent, _, _ = await legacy(db_session)
    report = await export_review(db_session)
    report["approved_fingerprint"] = report["fingerprint"]
    agent.last_consumed_at = None
    await db_session.flush()
    with pytest.raises(ValueError, match="Database changed"):
        await apply_review(db_session, report)
    assert await db_session.scalar(select(AgentPublicationProgress.id)) is None


async def test_approval_and_integrity_are_required(db_session):
    await legacy(db_session)
    report = await export_review(db_session)
    with pytest.raises(ValueError, match="approved_fingerprint"):
        await apply_review(db_session, report)
    report["approved_fingerprint"] = report["fingerprint"]
    report["agents"][0]["pending_resource_ids"] = []
    with pytest.raises(ValueError, match="Invalid review"):
        await apply_review(db_session, report)
