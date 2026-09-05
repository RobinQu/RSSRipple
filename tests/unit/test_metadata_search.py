"""Service-level tests for the single refresh execution path.

``refresh_work_by_source`` (search → pick → apply) replaced the old
``refresh_work_metadata``: these are the ported regression tests — LLM
candidate variance tolerance, manual-edit protection, season-scoped dates,
season-0 skip, identity creator-wins/no-steal — plus identity bagging.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.external_ids import find_work_by_external_id
from app.services.metadata_search import refresh_work_by_source

_SEARCH = "app.services.metadata_service.search_metadata_via_llm"
_POSTER = "app.services.metadata_search.download_and_cache_poster"
_AVAILABLE = "app.services.metadata_search.is_metadata_source_available"


def _uuid() -> str:
    return str(uuid.uuid4())


def _patch_search(candidates):
    return [
        patch(_SEARCH, new_callable=AsyncMock, return_value=candidates),
        patch(_POSTER, new_callable=AsyncMock, return_value=None),
        patch(_AVAILABLE, return_value=True),
    ]


async def test_refresh_tolerates_llm_candidate_variance(db_session):
    """LLM candidates may include season-ambiguity dicts or stray strings —
    refresh must skip them instead of 500ing (integration flake turned bug)."""
    work = TVSeries(
        id=_uuid(), title_en="Refresh Show", content_type="tv",
        external_id="tmdb:refresh-1", external_source="tmdb",
    )
    db_session.add(work)
    await db_session.flush()
    candidates = [
        "a stray string",
        {"season": 2},
        {"title_en": "Refresh Show", "content_type": "tv", "genre": ["Anime"]},
    ]
    patches = _patch_search(candidates)
    with patches[0], patches[1], patches[2]:
        result = await refresh_work_by_source(db_session, work, "tv", "tmdb")
    # The junk entries have no trusted identity → not selectable; the stray
    # string must not crash the normalization loop. No candidate → clean miss.
    assert result["found"] is False
    assert work.genre is None


async def test_refresh_skips_manually_edited_fields(db_session):
    """Refresh fills empty fields but never ones the user edited manually,
    unless override_manual_edits is passed."""
    work = TVSeries(
        id=_uuid(), title_en="Manual Show", content_type="tv",
        external_id="tmdb:manual-1", external_source="tmdb",
        manually_edited_fields=["rating", "genre"],
    )
    db_session.add(work)
    await db_session.flush()
    candidate = {
        "title_en": "Manual Show",
        "content_type": "tv",
        "rating": 9.0,
        "genre": ["Animation"],
        "description": "A synopsis.",
        "external_id": "tmdb:manual-1", "external_source": "tmdb",
    }
    patches = _patch_search([candidate])
    with patches[0], patches[1], patches[2]:
        result = await refresh_work_by_source(db_session, work, "tv", "tmdb")

    assert "description" in result["applied"]
    assert "rating" not in result["applied"]
    assert "genre" not in result["applied"]
    assert work.rating is None
    assert work.genre is None

    patches = _patch_search([candidate])
    with patches[0], patches[1], patches[2]:
        result2 = await refresh_work_by_source(
            db_session, work, "tv", "tmdb", override_manual_edits=True,
        )
    assert "rating" in result2["applied"]
    assert "genre" in result2["applied"]
    assert work.rating == 9.0
    assert work.genre == ["Animation"]


async def test_refresh_season_scoped_dates(db_session):
    """Per-season works: refresh passes the work's season to the source search
    and fills only season-scoped dates — a series-level entity's premiere
    belongs to season 1 and must not land on a later-season work."""
    work = TVSeries(
        id=_uuid(), title_cn="女性向遊戲世界對路人角色很不友好",
        content_type="tv", season_number=2,
    )
    db_session.add(work)
    await db_session.flush()
    candidate = {
        "title_cn": "恋爱游戏世界对路人角色很不友好 第二季",
        "content_type": "tv",
        "external_id": "bangumi:412144", "external_source": "bangumi",
        "start_date": "2026-07-08",
    }
    patches = _patch_search([candidate])
    with patches[0] as search, patches[1], patches[2]:
        result = await refresh_work_by_source(db_session, work, "tv", "bangumi")
    # Season-granularity entity: its own date is the season premiere, and the
    # search received the work's season as a hint.
    assert search.await_args.kwargs["season_hint"] == 2
    assert work.start_date == date(2026, 7, 8)
    assert "start_date" in result["applied"]
    # Identity is creator-wins and bag-only: the primary columns stay empty
    # (the work was created without one) and the id lands in the bag.
    assert work.external_id is None
    assert work.external_source is None
    owner = await find_work_by_external_id(db_session, "series", "bangumi", "bangumi:412144")
    assert owner is not None and owner.id == work.id

    series_entity = {
        "title_en": "Multi Season Show", "content_type": "tv",
        "external_id": "wikipedia:en:42", "external_source": "wikipedia",
        "start_date": "2020-01-01", "end_date": "2023-06-30",
    }
    # Series-level entity without per-season evidence → no date fill on a
    # season-2 work (previously the S1 premiere was stuffed into it).
    work2 = TVSeries(
        id=_uuid(), title_en="Multi Season Show", content_type="tv",
        season_number=2,
    )
    db_session.add(work2)
    await db_session.flush()
    patches = _patch_search([series_entity])
    with patches[0], patches[1], patches[2]:
        result2 = await refresh_work_by_source(db_session, work2, "tv", "wikipedia")
    assert work2.start_date is None
    assert "start_date" not in result2["applied"]

    # With per-season evidence the season's own air date is filled.
    work3 = TVSeries(
        id=_uuid(), title_en="Multi Season Show", content_type="tv",
        season_number=2,
    )
    db_session.add(work3)
    await db_session.flush()
    entity_with_seasons = {
        **series_entity,
        "external_id": "wikipedia:en:43",
        "seasons": [
            {"season_number": 1, "air_date": "2020-01-01"},
            {"season_number": 2, "air_date": "2022-04-01"},
        ],
    }
    patches = _patch_search([entity_with_seasons])
    with patches[0], patches[1], patches[2]:
        await refresh_work_by_source(db_session, work3, "tv", "wikipedia")
    assert work3.start_date == date(2022, 4, 1)


async def test_refresh_skips_season0_specials(db_session):
    """Season-0 works are specials placeholders: refresh must not match the
    main entry and stuff series-level data (dates/counts/identity) into them."""
    work = TVSeries(
        id=_uuid(), title_cn="某作品 OVA", content_type="tv", season_number=0,
    )
    db_session.add(work)
    await db_session.flush()
    with patch(_SEARCH, new_callable=AsyncMock) as search:
        result = await refresh_work_by_source(db_session, work, "tv", "bangumi")
    assert result["applied"] == []
    assert "season-0" in result["message"]
    search.assert_not_awaited()


async def test_refresh_season0_fills_start_date_from_sibling(db_session):
    """The season-0 skip still converges a NULL start_date deterministically
    from the collection's non-specials members — without any network search,
    and respecting manually_edited_fields."""
    coll = WorkCollection(
        id=_uuid(), title_cn="某作品", external_source="series_group",
    )
    s1 = TVSeries(
        id=_uuid(), title_cn="某作品", content_type="tv", season_number=1,
        collection_id=coll.id, start_date=date(2020, 1, 1),
    )
    work = TVSeries(
        id=_uuid(), title_cn="某作品 OVA", content_type="tv", season_number=0,
        collection_id=coll.id,
    )
    manual = TVSeries(
        id=_uuid(), title_cn="某作品 SP2", content_type="tv", season_number=0,
        collection_id=coll.id, manually_edited_fields=["start_date"],
    )
    db_session.add_all([coll, s1, work, manual])
    await db_session.flush()
    with patch(_SEARCH, new_callable=AsyncMock) as search:
        result = await refresh_work_by_source(db_session, work, "tv", "bangumi")
        result2 = await refresh_work_by_source(db_session, manual, "tv", "bangumi")
    assert result["applied"] == ["start_date"]
    assert work.start_date == date(2020, 1, 1)
    assert "season-0" in result["message"]
    assert result2["applied"] == []
    assert manual.start_date is None
    search.assert_not_awaited()


async def test_refresh_never_steals_identity(db_session):
    """A candidate identity already owned by another work (column or bag) is
    skipped, not grabbed — season-blind matches must not create duplicates."""
    owner = TVSeries(
        id=_uuid(), title_cn="Owned Show", content_type="tv", season_number=1,
        external_id="wikipedia:en:10380", external_source="wikipedia",
    )
    work = TVSeries(
        id=_uuid(), title_cn="Owned Show", content_type="tv", season_number=2,
    )
    db_session.add_all([owner, work])
    await db_session.flush()
    candidate = {
        "title_cn": "Owned Show", "content_type": "tv",
        "external_id": "wikipedia:en:10380", "external_source": "wikipedia",
        "start_date": "2011-04-06",
    }
    patches = _patch_search([candidate])
    with patches[0], patches[1], patches[2]:
        result = await refresh_work_by_source(db_session, work, "tv", "bangumi")
    assert result["identity_conflict"] is True
    assert result["applied"] == []
    assert work.external_id is None


async def test_refresh_identity_is_bag_only_even_with_override(db_session):
    """The candidate identity never lands on the primary columns through the
    refresh pipeline — even when they are empty and override_manual_edits is
    set; it only enters the WorkExternalId bag."""
    work = TVSeries(
        id=_uuid(), title_en="Ext Show", content_type="tv",
        external_id=None, external_source=None,
        manually_edited_fields=["external_id", "external_source"],
    )
    db_session.add(work)
    await db_session.flush()
    candidate = {
        "title_en": "Ext Show",
        "content_type": "tv",
        "external_id": "tmdb:555",
        "external_source": "tmdb",
    }
    patches = _patch_search([candidate])
    with patches[0], patches[1], patches[2]:
        await refresh_work_by_source(db_session, work, "tv", "tmdb")
    assert work.external_id is None
    assert work.external_source is None
    owner = await find_work_by_external_id(db_session, "series", "tmdb", "tmdb:555")
    assert owner is not None and owner.id == work.id

    patches = _patch_search([candidate])
    with patches[0], patches[1], patches[2]:
        await refresh_work_by_source(
            db_session, work, "tv", "tmdb", override_manual_edits=True,
        )
    assert work.external_id is None
    assert work.external_source is None


async def test_refresh_never_overwrites_existing_identity(db_session):
    """Creator-wins: an existing primary external_id is never replaced by the
    candidate's — the new id only enters the identity bag."""
    work = TVSeries(
        id=_uuid(), title_en="Old Id Show", content_type="tv",
        external_id="tmdb:old-1", external_source="tmdb",
    )
    db_session.add(work)
    await db_session.flush()
    candidate = {
        "title_en": "Old Id Show", "content_type": "tv",
        "external_id": "bangumi:777", "external_source": "bangumi",
    }
    patches = _patch_search([candidate])
    with patches[0], patches[1], patches[2]:
        await refresh_work_by_source(db_session, work, "tv", "bangumi")
    assert work.external_id == "tmdb:old-1"
    assert work.external_source == "tmdb"
    owner = await find_work_by_external_id(db_session, "series", "bangumi", "bangumi:777")
    assert owner is not None and owner.id == work.id
