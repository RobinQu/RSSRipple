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

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.schemas.metadata_search import MetadataCandidate, MetadataSearchRequest
from app.services.external_ids import add_external_id, find_work_by_external_id
from app.services.metadata_search import (
    _candidate_from_result,
    apply_work_metadata,
    preview_work_metadata,
    refresh_work_by_source,
    search_metadata_candidates,
)

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


# ---------------------------------------------------------------------------
# search / preview / apply — candidate construction, fallbacks, error branches
# ---------------------------------------------------------------------------


def _external_candidate(title="Show", *, external_id="tmdb:1", poster_url=None, metadata=None):
    return MetadataCandidate(
        origin="external",
        content_type="tv",
        title_cn=title,
        primary_source="tmdb",
        identity_source="tmdb",
        external_id=external_id,
        match_path="primary",
        selectable=True,
        poster_url=poster_url,
        metadata=metadata or {},
    )


def test_candidate_from_result_local_mode():
    """mode=local candidates carry origin/work_id."""
    request = MetadataSearchRequest(query="X", content_type="tv", mode="local")
    hit = _candidate_from_result(
        {"title_cn": "本地作品", "year": 2020, "_local_id": "w-1"}, request
    )
    assert hit.origin == "local"
    assert hit.work_id == "w-1"
    assert hit.match_path == "local"
    assert hit.selectable is True
    assert hit.title_cn == "本地作品"


def test_candidate_from_result_external_match_paths():
    """Web-fallback candidates are labeled by the registry source they resolved
    from; candidates without a trusted identity are not selectable."""
    req = MetadataSearchRequest(
        query="X", content_type="tv", mode="online", source="wikipedia"
    )
    primary = _candidate_from_result(
        {"title_en": "Show", "external_source": "wikipedia", "external_id": "wikipedia:en:1"},
        req,
    )
    assert primary.match_path == "primary"
    assert primary.selectable is True

    fallback = _candidate_from_result(
        {"title_en": "Show", "external_source": "tmdb", "external_id": "tmdb:2"},
        req,
    )
    assert fallback.match_path == "web_fallback"
    assert fallback.identity_source == "tmdb"
    assert fallback.primary_source == "wikipedia"

    no_id = _candidate_from_result({"title_en": "Show"}, req)
    assert no_id.selectable is False
    assert "no trusted external identity" in no_id.unavailable_reason


async def test_search_candidates_rejects_unavailable_online_source(db_session):
    """mode=online with a disabled source raises 400 before any search runs."""
    req = MetadataSearchRequest(query="Show", content_type="tv", mode="online", source="tmdb")
    with patch("app.services.metadata_search.is_metadata_source_available", return_value=False):
        with pytest.raises(HTTPException) as exc:
            await search_metadata_candidates(db_session, req)
    assert exc.value.status_code == 400


async def test_preview_work_not_found(db_session):
    with pytest.raises(HTTPException) as exc:
        await preview_work_metadata(db_session, _uuid(), "tv", _external_candidate(), False)
    assert exc.value.status_code == 404


async def test_apply_work_not_found(db_session):
    with pytest.raises(HTTPException) as exc:
        await apply_work_metadata(db_session, _uuid(), "tv", _external_candidate(), False)
    assert exc.value.status_code == 404


async def test_preview_ignores_badly_typed_values(db_session):
    """rating="not-a-number" / number_of_episodes="xyz" hit the safe-caster
    None branch (and the equal-value skip for title_cn) instead of 500ing."""
    work = TVSeries(id=_uuid(), title_cn="Show", content_type="tv")
    db_session.add(work)
    await db_session.flush()
    candidate = _external_candidate(metadata={
        "rating": "not-a-number",
        "number_of_episodes": "xyz",
    })
    result = await preview_work_metadata(db_session, work.id, "tv", candidate, False)
    assert result["changes"] == []


async def test_preview_poster_and_is_anime_changes(db_session):
    """poster_url / is_anime changes flow through the preview change list
    (only_missing=False → they are candidates for apply)."""
    work = TVSeries(id=_uuid(), title_cn="Show", content_type="tv")
    db_session.add(work)
    await db_session.flush()
    candidate = _external_candidate(
        poster_url="http://cdn.example.com/poster.jpg",
        metadata={"is_anime": True},
    )
    result = await preview_work_metadata(db_session, work.id, "tv", candidate, False)
    fields = {c["field"] for c in result["changes"]}
    assert "poster_url" in fields
    assert "is_anime" in fields


async def test_preview_only_missing_skips_existing_poster_and_is_anime(db_session):
    """only_missing=True never proposes changing an already-set poster/is_anime."""
    work = TVSeries(
        id=_uuid(), title_cn="Show", content_type="tv",
        is_anime=True, poster_url="http://cdn.example.com/old.jpg",
    )
    db_session.add(work)
    await db_session.flush()
    candidate = _external_candidate(
        poster_url="http://cdn.example.com/new.jpg",
        metadata={"is_anime": False},
    )
    result = await preview_work_metadata(
        db_session, work.id, "tv", candidate, False, only_missing=True
    )
    assert {c["field"] for c in result["changes"]} <= {"title_cn"}
    assert "poster_url" not in {c["field"] for c in result["changes"]}
    assert "is_anime" not in {c["field"] for c in result["changes"]}


async def test_apply_rejects_bagged_identity_conflict(db_session):
    """A candidate identity already bagged by another work → 409, never steal."""
    owner = TVSeries(id=_uuid(), title_cn="Owner", content_type="tv")
    work = TVSeries(id=_uuid(), title_cn="Show", content_type="tv")
    db_session.add_all([owner, work])
    await db_session.flush()
    await add_external_id(db_session, "series", owner.id, "tmdb", "tmdb:999")
    candidate = _external_candidate(external_id="tmdb:999")
    with pytest.raises(HTTPException) as exc:
        await apply_work_metadata(db_session, work.id, "tv", candidate, False)
    assert exc.value.status_code == 409


async def test_apply_rejects_cross_type_identity_conflict(db_session):
    """A candidate identity bagged for the OTHER work type → 409."""
    movie = Movie(id=_uuid(), title_cn="电影", content_type="movie")
    work = TVSeries(id=_uuid(), title_cn="Show", content_type="tv")
    db_session.add_all([movie, work])
    await db_session.flush()
    await add_external_id(db_session, "movie", movie.id, "tmdb", "tmdb:777")
    candidate = _external_candidate(external_id="tmdb:777")
    with pytest.raises(HTTPException) as exc:
        await apply_work_metadata(db_session, work.id, "tv", candidate, False)
    assert exc.value.status_code == 409


async def test_apply_full_success_poster_is_anime_episodes(db_session):
    """A selectable candidate applies poster + is_anime + season-scoped Episode
    rows, and the poster cache miss falls back to the candidate's URL."""
    work = TVSeries(id=_uuid(), title_cn="Show", content_type="tv", season_number=1)
    db_session.add(work)
    await db_session.flush()
    candidate = _external_candidate(
        poster_url="http://cdn.example.com/p.jpg",
        metadata={
            "is_anime": True,
            "episode_list": [{"season": 1, "episode": 1, "title": "Ep1"}],
        },
    )
    with patch(
        "app.services.metadata_search.download_and_cache_poster",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await apply_work_metadata(db_session, work.id, "tv", candidate, False)
    assert "poster_url" in result["applied"]
    assert "is_anime" in result["applied"]
    assert work.poster_url == "http://cdn.example.com/p.jpg"
    assert work.is_anime is True
    from app.models.episode import Episode

    eps = (await db_session.execute(select(Episode))).scalars().all()
    assert [(e.season, e.episode) for e in eps] == [(1, 1)]


async def test_apply_override_manual_edits_forces_is_anime(db_session):
    """override_manual_edits=True writes the candidate's is_anime verdict
    directly instead of going through the sticky apply_is_anime policy."""
    work = TVSeries(id=_uuid(), title_cn="Show", content_type="tv", is_anime=True)
    db_session.add(work)
    await db_session.flush()
    candidate = _external_candidate(metadata={"is_anime": False})
    result = await apply_work_metadata(
        db_session, work.id, "tv", candidate, override_manual_edits=True
    )
    assert "is_anime" in result["applied"]
    assert work.is_anime is False


async def test_refresh_work_none(db_session):
    result = await refresh_work_by_source(db_session, None, "tv", "tmdb")
    assert result["found"] is False
    assert "work not found" in result["message"]


async def test_refresh_no_title_available(db_session):
    work = TVSeries(id=_uuid(), content_type="tv")
    db_session.add(work)
    await db_session.flush()
    with patch(_SEARCH, new_callable=AsyncMock) as search:
        result = await refresh_work_by_source(db_session, work, "tv", "tmdb")
    assert result["found"] is False
    assert "no title" in result["message"]
    search.assert_not_awaited()


async def test_refresh_re_raises_non_conflict_http(db_session):
    """Only 409 (identity conflict) is swallowed into the result dict — other
    HTTPExceptions from apply must propagate."""
    work = TVSeries(id=_uuid(), title_en="Show", content_type="tv")
    db_session.add(work)
    await db_session.flush()
    patches = _patch_search([
        {"title_en": "Show", "content_type": "tv",
         "external_id": "tmdb:5", "external_source": "tmdb"},
    ])
    with patches[0], patches[1], patches[2]:
        with patch(
            "app.services.metadata_search.apply_work_metadata",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=400, detail="boom"),
        ) as ap:
            with pytest.raises(HTTPException) as exc:
                await refresh_work_by_source(db_session, work, "tv", "tmdb")
    assert exc.value.status_code == 400
    ap.assert_awaited_once()
