"""Startup gates on real database state, before application background work."""

import pytest
from sqlalchemy import delete

from app.models.app_setting import AppSetting
from app.models.resource_publication import ResourcePublication
from app.services.publication_migration import MARKER, bootstrap_publications
from app.services.publication_startup import ensure_publication_ready
from tests.unit.test_publication_migration import legacy


async def test_empty_database_initialization_is_repeatable(db_session):
    await ensure_publication_ready(db_session)
    assert (await db_session.get(AppSetting, MARKER)).value == "fresh"
    await ensure_publication_ready(db_session)


async def test_old_database_requires_migration_then_starts(db_session):
    await legacy(db_session)
    with pytest.raises(ValueError, match="migration required"):
        await ensure_publication_ready(db_session)
    assert await db_session.get(AppSetting, MARKER) is None
    await bootstrap_publications(db_session, writers_stopped=True)
    await ensure_publication_ready(db_session)


async def test_marker_cannot_hide_a_resource_written_without_event(db_session):
    _, _, rows = await legacy(db_session)
    await bootstrap_publications(db_session, writers_stopped=True)
    await db_session.execute(delete(ResourcePublication).where(ResourcePublication.resource_id == rows[1].id))
    with pytest.raises(ValueError, match="without publication"):
        await ensure_publication_ready(db_session)
