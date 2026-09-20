"""Replay unchanged captured resources through production coverage policy.

The export predates season splitting. Its three decisions are already decided,
so this test does not claim production pending-choice/concurrency evidence.
"""
import hashlib

import pytest
from sqlalchemy import event, select

from app.models.file_resource import FileResource
from app.models.pending_decision import PendingDecision
from app.services.resource_confirmation import inspect_resource_confirmation
from app.services.resource_coverage import batch_coverage, load_batch_coverage

from .loader import FIXTURE_PATH

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_captured_assignment_evidence_overrides_identical_title_bounds(fixture_db):
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == (
        "d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32"
    )
    complete_id = "5874f971-d15a-4e0f-b85b-26f85475cadc"
    incomplete_id = "e8a58372-555a-4e7c-b8b7-37371989c040"
    work_id = "3fe8ede1-673c-49ce-8be4-d373eed0e4e4"
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().upper())

    event.listen(fixture_db.engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with fixture_db.factory() as db:
            resources = list(await db.scalars(select(FileResource).where(FileResource.is_batch.is_(True))))
            assert len(resources) == 101
            await load_batch_coverage(db, resources)
            by_id = {r.id: r for r in resources}
            complete, incomplete = by_id[complete_id], by_id[incomplete_id]
            for resource in (complete, incomplete):
                assert (resource.series_id, resource.season, resource.episode_start, resource.episode_end) == (
                    work_id, 1, 1, 7
                )
                assert len(resource.file_assignments) == 7
            assert batch_coverage(complete) == (("series", work_id, 1, ((1, 7),)),)
            assert batch_coverage(incomplete) is None
            assert "batch_coverage_unknown" not in inspect_resource_confirmation(complete, None).kinds
            assert "batch_coverage_unknown" in inspect_resource_confirmation(incomplete, None).kinds
            # Keep historical single-candidate decisions untouched: they are
            # terminal evidence, not pending candidates to manufacture or rekey.
            history = list(await db.scalars(select(PendingDecision)))
            expected = fixture_db.data["tables"]["pending_decisions"]
            assert len(history) == len(expected) == 3
            assert {r.id: (r.status, r.candidates) for r in history} == {
                r["id"]: (r["status"], r["candidates"]) for r in expected
            }
            assert all(r.status == "decided" and len(r.candidates) == 1 for r in history)
            assert not db.new and not db.dirty and not db.deleted
    finally:
        event.remove(fixture_db.engine.sync_engine, "before_cursor_execute", capture)
    assert statements and all(s.startswith(("SELECT", "PRAGMA")) for s in statements)
