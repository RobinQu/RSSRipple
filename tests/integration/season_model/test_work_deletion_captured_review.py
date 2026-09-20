"""Deletion necessity on the unchanged captured graph, not synthetic mappings."""

import hashlib

import httpx
import pytest
from sqlalchemy import select

from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from tests.api.conftest import _build_test_app

from .conftest import open_fixture_db
from .loader import FIXTURE_PATH

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.mark.parametrize(
    "work_id,model",
    [
        ("1d721de9-dbf9-46af-977a-fda92eb336c1", ResourceWorkLink),
        ("4bc342c7-4701-4d95-afbe-0e6b976cd9bd", ResourceFileAssignment),
    ],
)
async def test_captured_manual_evidence_survives_unreviewed_work_delete(tmp_path, work_id, model):
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == (
        "d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32"
    )
    async with open_fixture_db(tmp_path / "captured-deletion.db") as handle:
        async with handle.factory() as db:
            assert await db.get(TVSeries, work_id) is not None
            before = set(await db.scalars(select(model.id).where(model.series_id == work_id, model.source == "manual")))
            assert before
        app = _build_test_app(handle.factory)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(f"/api/v1/series/{work_id}")
        async with handle.factory() as db:
            retained = set(await db.scalars(select(model.id).where(model.id.in_(before))))
            work_retained = await db.get(TVSeries, work_id) is not None
        assert response.status_code == 409 and retained == before and work_retained, {
            "status": response.status_code,
            "captured_manual_rows": len(before),
            "retained_rows": len(retained),
            "work_retained": work_retained,
        }
