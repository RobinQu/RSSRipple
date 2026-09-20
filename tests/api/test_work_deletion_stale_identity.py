"""Deletion identity invariant using real DB/API and synthetic registry IDs."""

import pytest
from sqlalchemy import select

from app.models.work_external_id import WorkExternalId
from app.services.external_ids import add_external_id


@pytest.mark.parametrize("kind", ["series", "movie"])
async def test_stale_identity_registration_cannot_recreate_deleted_owner(
    client, db_session_factory, sample_series, sample_movie, kind
):
    work = sample_series if kind == "series" else sample_movie
    route = "series" if kind == "series" else "movies"
    response = await client.delete(f"/api/v1/{route}/{work.id}")
    assert response.status_code == 200, response.text
    # A worker that captured the identity before DELETE must not insert an orphan
    # after DELETE commits. This is a deterministic ordering, not a concurrency simulation.
    async with db_session_factory() as db:
        registered = await add_external_id(db, kind, work.id, "tmdb", "987654323")
        await db.commit()
    async with db_session_factory() as db:
        orphan = await db.scalar(select(WorkExternalId.id).where(
            WorkExternalId.work_type == kind, WorkExternalId.work_id == work.id
        ))
        assert not registered and orphan is None
