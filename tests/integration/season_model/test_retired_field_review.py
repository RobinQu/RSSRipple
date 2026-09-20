"""Replay the captured production graph without rewriting its legacy evidence."""

import copy
import json

import pytest
from sqlalchemy import event, select

from app.models.series import TVSeries
from app.services.collection_lifecycle import backfill_orphan_collections
from scripts.retired_season_fields import apply_review, export_reviews, review_work

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_captured_graph_audit_and_reviewed_cleanup(fixture_db, tmp_path):
    expected = {
        row["id"]: row for row in fixture_db.data["tables"]["tv_series"] if row.get("number_of_seasons") is not None
    }
    assert len(expected) == 27  # frozen source fixture, not a live-production claim
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().upper())

    output = tmp_path / "captured-review.jsonl"
    event.listen(fixture_db.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert await export_reviews(str(output)) == len(expected)
    finally:
        event.remove(fixture_db.engine.sync_engine, "before_cursor_execute", capture)
    assert statements and all(s.startswith(("SELECT", "PRAGMA")) for s in statements)
    reviews = [json.loads(line) for line in output.read_text().splitlines()]
    assert {r["work_id"] for r in reviews} == set(expected)
    for review in reviews:
        assert review["snapshot"]["work"]["number_of_seasons"] == expected[review["work_id"]]["number_of_seasons"]
        assert review["snapshot"]["work"]["seasons"] == expected[review["work_id"]].get("seasons")
    legacy = next(r for r in reviews if len(r["snapshot"]["work"]["seasons"] or []) > 1)
    legacy["confirmed_season"] = legacy["snapshot"]["work"]["season_number"]
    async with fixture_db.factory() as db:
        before = await review_work(db, legacy["work_id"])
        with pytest.raises(ValueError):
            await apply_review(db, legacy)
        assert await review_work(db, legacy["work_id"]) == before

    # Actual V7 orphan repair, not a test-side rewrite or manufactured season choice.
    # Genuine multi-season rows stay in the report for separately reviewed splitting.
    await backfill_orphan_collections()
    async with fixture_db.factory() as db:
        ids = list(await db.scalars(select(TVSeries.id).where(TVSeries.number_of_seasons.is_not(None))))
        assert set(ids) == set(expected)
        cleared = set()
        for identity in ids:
            review = await review_work(db, identity)
            work = review["snapshot"]["work"]
            if review["blocked_reasons"] or work["number_of_seasons"] != 1 or not work["seasons"]:
                assert work["number_of_seasons"] == expected[identity]["number_of_seasons"]
                continue  # no operator confirmation for ambiguous/multi-season rows
            assert all(s["season_number"] == work["season_number"] for s in work["seasons"])
            review["confirmed_season"] = work["season_number"]
            before = copy.deepcopy(review["snapshot"])
            assert (await apply_review(db, review))["changed"] is True
            after = (await review_work(db, identity))["snapshot"]
            for key in before.keys() - {"work"}:
                assert after[key] == before[key]
            for key in before["work"].keys() - {"number_of_seasons", "manually_edited_fields", "updated_at"}:
                assert after["work"][key] == before["work"][key]
            assert (await apply_review(db, review))["changed"] is False
            cleared.add(identity)
        print({"captured_retired_rows": len(expected), "reviewed_single_season_rows_cleared": len(cleared)})
        assert len(cleared) == 8
        await db.commit()
        counts = dict((await db.execute(select(TVSeries.id, TVSeries.number_of_seasons))).all())
        for identity, original in expected.items():
            assert counts[identity] == (None if identity in cleared else original["number_of_seasons"])
