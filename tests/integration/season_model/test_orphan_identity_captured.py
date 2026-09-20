"""The unmodified captured graph is a negative control for orphan cleanup."""

import hashlib

import pytest
from sqlalchemy import select

from app.models.work_external_id import WorkExternalId
from scripts.review_orphan_identities import export_review

from .conftest import open_fixture_db
from .loader import FIXTURE_PATH

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_captured_identity_review_is_empty_and_preserves_rows(tmp_path):
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == (
        "d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32"
    )
    async with open_fixture_db(tmp_path / "captured-identities.db") as handle:
        async with handle.factory() as db:
            before = list((await db.execute(select(WorkExternalId.__table__).order_by(WorkExternalId.id))).mappings())
            assert len(before) == 249
            report = await export_review(db)
            assert report["orphans"] == [] and report["blocked"] == []
            await db.commit()
        async with handle.factory() as db:
            after = list((await db.execute(select(WorkExternalId.__table__).order_by(WorkExternalId.id))).mappings())
            assert after == before
