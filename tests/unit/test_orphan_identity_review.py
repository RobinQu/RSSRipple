"""Synthetic orphans against a real database, not mutated captured records."""

import uuid

import pytest
from sqlalchemy import select

from app.models.movie import Movie
from app.models.work_external_id import WorkExternalId
from scripts.review_orphan_identities import apply_review, export_review


async def seed(db):
    owner = Movie(title_cn="Synthetic valid owner")
    db.add(owner)
    await db.flush()
    valid = WorkExternalId(work_type="movie", work_id=owner.id, source="tmdb", external_id="tmdb:1234")
    orphan = WorkExternalId(work_type="movie", work_id=str(uuid.uuid4()), source="tmdb", external_id="tmdb:5678")
    db.add_all([valid, orphan])
    await db.flush()
    review = await export_review(db)
    assert [row["id"] for row in review["orphans"]] == [orphan.id]
    review.update(approved_fingerprint=review["fingerprint"], selected_ids=[orphan.id])
    return valid, orphan, review


async def test_apply_only_selected_orphan_and_repeat(db_session):
    valid, orphan, review = await seed(db_session)
    assert (await apply_review(db_session, review))["deleted_ids"] == [orphan.id]
    assert await db_session.get(WorkExternalId, valid.id) is not None
    assert (await apply_review(db_session, review))["already_absent_ids"] == [orphan.id]


@pytest.mark.parametrize("change", ["owner", "row", "approval", "unknown"])
async def test_changed_or_unapproved_review_rejected(db_session, change):
    valid, orphan, review = await seed(db_session)
    if change == "owner":
        db_session.add(Movie(id=orphan.work_id, title_cn="Synthetic recovered owner"))
    elif change == "row":
        orphan.external_id = "tmdb:changed"
    elif change == "approval":
        review.pop("approved_fingerprint")
    else:
        db_session.add(
            WorkExternalId(work_type="unknown", work_id=str(uuid.uuid4()), source="tmdb", external_id="tmdb:unknown")
        )
    await db_session.flush()
    with pytest.raises(ValueError):
        await apply_review(db_session, review)
    assert set(await db_session.scalars(select(WorkExternalId.id))) >= {valid.id, orphan.id}


async def test_outer_failure_restores_deleted_orphan(db_session):
    _, orphan, review = await seed(db_session)
    oid = orphan.id
    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            await apply_review(db_session, review)
            raise RuntimeError("synthetic after deletion failure")
    assert await db_session.scalar(select(WorkExternalId.id).where(WorkExternalId.id == oid)) == oid
