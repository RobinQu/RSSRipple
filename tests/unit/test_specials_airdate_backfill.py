"""Dry-run evaluation logic of scripts/specials_airdate_backfill.py."""

from __future__ import annotations

import uuid
from datetime import date

from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from scripts.specials_airdate_backfill import (
    apply_report,
    evaluate_work,
    select_candidate_works,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _work(collection_id, season, **overrides):
    base = dict(
        id=_uuid(), title_cn="剧集X", content_type="tv",
        season_number=season, collection_id=collection_id,
    )
    base.update(overrides)
    return TVSeries(**base)


async def test_select_candidates_only_null_start_date_in_collection(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="合集X", external_source="series_group")
    dated = _work(coll.id, 1, start_date=date(2020, 1, 1))
    orphan = TVSeries(id=_uuid(), title_cn="无合集", content_type="tv", season_number=0)
    sp = _work(coll.id, 0)
    db_session.add_all([coll, dated, orphan, sp])
    await db_session.flush()
    candidates = await select_candidate_works(db_session)
    assert [w.id for w in candidates] == [sp.id]


async def test_evaluate_and_apply_fills_from_earliest_regular_sibling(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="合集X", external_source="series_group")
    db_session.add(coll)
    db_session.add_all([
        _work(coll.id, 2, start_date=date(2022, 4, 1)),
        _work(coll.id, 1, start_date=date(2020, 1, 1)),
    ])
    sp = _work(coll.id, 0)
    db_session.add(sp)
    await db_session.flush()

    rep = await evaluate_work(db_session, sp)
    assert rep["ok"] is True
    assert rep["start_date"] == "2020-01-01"
    assert rep["season"] == 0
    # Dry-run evaluation writes nothing; apply_report does (NULL only).
    assert sp.start_date is None
    assert apply_report(sp, rep) == ["start_date"]
    assert sp.start_date == date(2020, 1, 1)
    # An already-dated work is never overwritten.
    assert apply_report(sp, rep) == []
    assert sp.start_date == date(2020, 1, 1)


async def test_evaluate_skips_manual_edit_and_missing_sibling_dates(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="合集X", external_source="series_group")
    manual = _work(coll.id, 0, manually_edited_fields=["start_date"])
    db_session.add_all([coll, manual])
    await db_session.flush()
    rep = await evaluate_work(db_session, manual)
    assert rep["ok"] is False
    assert "manually edited" in rep["reason"]

    coll2 = WorkCollection(id=_uuid(), title_cn="合集Y", external_source="series_group")
    db_session.add(coll2)
    db_session.add_all([
        _work(coll2.id, 1),  # sibling without a date
        _work(coll2.id, 0, start_date=date(2019, 6, 1)),  # specials date: never borrowed
    ])
    lonely = _work(coll2.id, 0)
    db_session.add(lonely)
    await db_session.flush()
    rep2 = await evaluate_work(db_session, lonely)
    assert rep2["ok"] is False
    assert "no dated non-specials sibling" in rep2["reason"]
