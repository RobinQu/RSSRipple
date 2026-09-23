"""Bootstrap semantics using actual SQL; all row identities are synthetic."""

from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.models.agent_publication_progress import AgentPublicationProgress
from app.models.app_setting import AppSetting
from app.models.file_resource import FileResource
from app.models.resource_publication import ChannelPublicationCounter, ResourcePublication
from app.services.agent_publication_progress import snapshot_publications
from app.services.publication_migration import MARKER, bootstrap_publications
from app.utils.time import utcnow
from tests.unit.test_agent_publication_progress import setup


async def legacy(db):
    agent, old = await setup(db)
    await db.execute(delete(AgentPublicationProgress))
    await db.execute(delete(ResourcePublication))
    await db.execute(delete(ChannelPublicationCounter))
    now = utcnow()
    old.created_at = now
    agent.last_consumed_at = now
    # Same timestamp is excluded, strictly later resource remains consumable.
    rows = [
        FileResource(
            channel_id=old.channel_id,
            guid=f"migration-{i}",
            title_raw="Synthetic",
            torrent_url="synthetic",
            created_at=now + timedelta(seconds=i),
        )
        for i in (0, 1)
    ]
    db.add_all(rows)
    await db.flush()
    return agent, old, rows


async def test_bootstrap_preserves_timestamp_boundary_and_is_idempotent(db_session):
    agent, old, rows = await legacy(db_session)
    result = await bootstrap_publications(db_session, writers_stopped=True)
    assert result == {"status": "applied", "resources": 3, "agents": 1}
    snapshot = await snapshot_publications(db_session, agent.id, old.channel_id)
    assert snapshot.resource_ids == (rows[1].id,)
    assert await bootstrap_publications(db_session, writers_stopped=True) == {"status": "already_applied"}
    assert await snapshot_publications(db_session, agent.id, old.channel_id) == snapshot


async def test_failed_bootstrap_rolls_back_marker_events_and_progress(db_session):
    await legacy(db_session)
    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            await bootstrap_publications(db_session, writers_stopped=True)
            raise RuntimeError("Synthetic crash before transaction commit")
    assert await db_session.get(AppSetting, MARKER) is None
    assert await db_session.scalar(select(ResourcePublication.id)) is None
    assert await db_session.scalar(select(AgentPublicationProgress.id)) is None
    assert (await bootstrap_publications(db_session, writers_stopped=True))["status"] == "applied"


async def test_null_watermark_waits_for_first_run(db_session):
    agent, _, _ = await legacy(db_session)
    agent.last_consumed_at = None
    await db_session.flush()
    assert (await bootstrap_publications(db_session, writers_stopped=True))["agents"] == 0
    assert await db_session.scalar(select(AgentPublicationProgress.id)) is None


async def test_rejects_unmarked_publications_and_missing_maintenance_barrier(db_session):
    await setup(db_session)
    with pytest.raises(ValueError, match="stopped writers"):
        await bootstrap_publications(db_session, writers_stopped=False)
    with pytest.raises(ValueError, match="Unmarked"):
        await bootstrap_publications(db_session, writers_stopped=True)
    assert await db_session.get(AppSetting, MARKER) is None
