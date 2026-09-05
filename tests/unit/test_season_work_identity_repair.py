"""Tests for scripts/season_work_identity_repair.py (phase 1, offline)."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select

from app.models.series import TVSeries
from app.models.work_external_id import WorkExternalId
from scripts.season_work_identity_repair import (
    _backfill_targets,
    _find_identity_collisions,
    _strip_stolen_identity,
)


def _uuid() -> str:
    return str(uuid.uuid4())


async def test_collision_groups_sorted_lowest_season_first(db_session):
    s1 = TVSeries(
        id=_uuid(), title_cn="Show", content_type="tv", season_number=1,
        external_id="bangumi:1", external_source="bangumi",
        start_date=date(2020, 1, 1),
    )
    s2 = TVSeries(
        id=_uuid(), title_cn="Show", content_type="tv", season_number=2,
        external_id="bangumi:1", external_source="bangumi",
        start_date=date(2020, 1, 1),
    )
    unique = TVSeries(
        id=_uuid(), title_cn="Other", content_type="tv", season_number=1,
        external_id="bangumi:2", external_source="bangumi",
    )
    db_session.add_all([s2, s1, unique])
    await db_session.flush()

    groups = await _find_identity_collisions(db_session)
    assert len(groups) == 1
    assert [w.season_number for w in groups[0]] == [1, 2]


async def test_collision_owner_never_specials_over_season1(db_session):
    """S0 specials must not win the main entry's identity over season 1."""
    s0 = TVSeries(
        id=_uuid(), title_cn="Show", content_type="tv", season_number=0,
        external_id="bangumi:3", external_source="bangumi",
    )
    s1 = TVSeries(
        id=_uuid(), title_cn="Show", content_type="tv", season_number=1,
        external_id="bangumi:3", external_source="bangumi",
    )
    db_session.add_all([s0, s1])
    await db_session.flush()

    groups = await _find_identity_collisions(db_session)
    assert [w.season_number for w in groups[0]] == [1, 0]


async def test_strip_stolen_identity_clears_entity_fields_and_bag(db_session):
    owner = TVSeries(
        id=_uuid(), title_cn="Show", content_type="tv", season_number=1,
        external_id="bangumi:1", external_source="bangumi",
        start_date=date(2020, 1, 1), rating=7.0,
    )
    stolen = TVSeries(
        id=_uuid(), title_cn="Show", content_type="tv", season_number=2,
        external_id="bangumi:1", external_source="bangumi",
        start_date=date(2020, 1, 1), number_of_episodes=12, rating=7.0,
        genre=["Animation"], description="S1 synopsis",
    )
    db_session.add_all([owner, stolen])
    await db_session.flush()
    db_session.add(WorkExternalId(
        work_type="series", work_id=stolen.id,
        source="bangumi", external_id="bangumi:1",
    ))
    await db_session.flush()

    cleared = await _strip_stolen_identity(db_session, stolen, owner)
    assert stolen.external_id is None
    assert stolen.external_source is None
    assert stolen.start_date is None
    assert stolen.number_of_episodes is None
    assert stolen.rating is None
    assert stolen.genre is None
    assert stolen.description is None
    assert "bag:bangumi:1" in cleared
    remaining = (await db_session.execute(
        select(WorkExternalId).where(WorkExternalId.work_id == stolen.id)
    )).scalars().all()
    assert remaining == []
    # Owner row untouched.
    assert owner.external_id == "bangumi:1"
    assert owner.start_date == date(2020, 1, 1)


async def test_strip_stolen_identity_respects_manual_edits(db_session):
    owner = TVSeries(
        id=_uuid(), title_cn="Show", content_type="tv", season_number=1,
        external_id="bangumi:1", external_source="bangumi",
    )
    stolen = TVSeries(
        id=_uuid(), title_cn="Show", content_type="tv", season_number=3,
        external_id="bangumi:1", external_source="bangumi",
        rating=9.9, manually_edited_fields=["rating"],
    )
    db_session.add_all([owner, stolen])
    await db_session.flush()

    await _strip_stolen_identity(db_session, stolen, owner)
    assert stolen.external_id is None
    assert stolen.rating == 9.9  # manually edited — preserved


async def test_backfill_targets_fall_back_to_collection_sibling_source(db_session):
    from app.models.work_collection import WorkCollection

    coll = WorkCollection(id=_uuid(), title_cn="Show")
    s1 = TVSeries(
        id=_uuid(), title_cn="Show", content_type="tv", season_number=1,
        external_id="bangumi:9", external_source="bangumi",
        start_date=date(2020, 1, 1), collection_id=coll.id,
    )
    shell = TVSeries(
        id=_uuid(), title_cn="Show", content_type="tv", season_number=2,
        collection_id=coll.id,
    )
    orphan = TVSeries(id=_uuid(), title_cn="Orphan", content_type="tv", season_number=1)
    dated = TVSeries(
        id=_uuid(), title_cn="Dated", content_type="tv", season_number=1,
        start_date=date(2021, 1, 1),
    )
    db_session.add_all([coll, s1, shell, orphan, dated])
    await db_session.flush()

    targets = {t["id"]: t for t in await _backfill_targets(db_session)}
    # Only works missing start_date are targets.
    assert set(targets) == {shell.id, orphan.id}
    # The shell inherits the collection sibling's bangumi source.
    assert targets[shell.id]["source"] == "bangumi"
    # No identity anywhere → skipped downstream (source None).
    assert targets[orphan.id]["source"] is None
