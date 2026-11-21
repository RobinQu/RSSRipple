"""In-process integration coverage for the metadata search/preview/apply service.

Runs against the shared per-test Turso DB (``tests/integration/metadata``
reuses the unit DB fixtures); external source search and poster I/O are
patched at the module boundary so the test is hermetic and needs no API keys.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.episode import Episode
from app.models.series import TVSeries
from app.schemas.metadata_search import MetadataCandidate, MetadataSearchRequest
from app.services.external_ids import add_external_id
from app.services.metadata_search import (
    apply_work_metadata,
    preview_work_metadata,
    refresh_work_by_source,
    search_metadata_candidates,
)

_SEARCH = "app.services.metadata_search.manual_search_metadata"
_POSTER = "app.services.metadata_search.download_and_cache_poster"
_AVAILABLE = "app.services.metadata_search.is_metadata_source_available"


def _uuid() -> str:
    return str(uuid.uuid4())


def _candidate(
    title: str = "Show",
    *,
    external_id: str = "tmdb:1",
    poster_url: str | None = None,
    metadata: dict | None = None,
) -> MetadataCandidate:
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


async def test_search_candidates_online_and_local(db_session):
    request = MetadataSearchRequest(
        query="Show", content_type="tv", mode="online", source="tmdb"
    )
    results = [
        {
            "content_type": "tv",
            "title_cn": "Show",
            "external_source": "tmdb",
            "external_id": "tmdb:1",
        },
        {
            "content_type": "tv",
            "title_cn": "Fallback",
            "external_source": "wikipedia",
            "external_id": "wikipedia:en:2",
        },
        {"content_type": "tv", "title_cn": "No identity"},
        {
            "content_type": "movie",
            "title_cn": "Wrong type",
            "external_source": "tmdb",
            "external_id": "tmdb:9",
        },
    ]
    with patch(_AVAILABLE, return_value=True), patch(
        _SEARCH, new_callable=AsyncMock, return_value=results
    ):
        candidates = await search_metadata_candidates(db_session, request)

    assert [c.title_cn for c in candidates] == ["Show", "Fallback", "No identity"]
    assert candidates[0].match_path == "primary"
    assert candidates[0].selectable is True
    assert candidates[1].match_path == "web_fallback"
    assert candidates[1].identity_source == "wikipedia"
    assert candidates[2].selectable is False

    local_request = MetadataSearchRequest(query="X", content_type="tv", mode="local")
    local_results = [
        {"_local_id": "w-1", "content_type": "tv", "title_cn": "Local"},
        {"content_type": "tv", "title_cn": "No local id"},
    ]
    with patch(_SEARCH, new_callable=AsyncMock, return_value=local_results):
        local_candidates = await search_metadata_candidates(db_session, local_request)
    assert [c.work_id for c in local_candidates] == ["w-1"]
    assert local_candidates[0].origin == "local"
    assert local_candidates[0].match_path == "local"


async def test_search_rejects_unavailable_online_source(db_session):
    request = MetadataSearchRequest(
        query="Show", content_type="tv", mode="online", source="tmdb"
    )
    with patch(_AVAILABLE, return_value=False), pytest.raises(HTTPException) as exc:
        await search_metadata_candidates(db_session, request)
    assert exc.value.status_code == 400


async def test_preview_and_apply_metadata(db_session):
    work = TVSeries(id=_uuid(), title_cn="Show", content_type="tv", season_number=1)
    db_session.add(work)
    await db_session.flush()
    candidate = _candidate(
        poster_url="http://cdn.example.com/p.jpg",
        metadata={
            "is_anime": True,
            "rating": "8.5",
            "number_of_episodes": "12",
            "description": "Desc",
            "genre": ["Action"],
            "episode_list": [{"season": 1, "episode": 1, "title": "Ep1"}],
        },
    )

    preview = await preview_work_metadata(db_session, work.id, "tv", candidate, False)
    fields = {change["field"] for change in preview["changes"]}
    assert {
        "poster_url",
        "is_anime",
        "rating",
        "number_of_episodes",
        "description",
        "genre",
    } <= fields

    with patch(_POSTER, new_callable=AsyncMock, return_value=None):
        result = await apply_work_metadata(db_session, work.id, "tv", candidate, False)

    assert "poster_url" in result["applied"]
    assert "is_anime" in result["applied"]
    assert work.poster_url == "http://cdn.example.com/p.jpg"
    assert work.is_anime is True
    episodes = (await db_session.execute(select(Episode))).scalars().all()
    assert [(row.season, row.episode) for row in episodes] == [(1, 1)]


async def test_preview_only_missing_and_badly_typed_values(db_session):
    work = TVSeries(
        id=_uuid(),
        title_cn="Show",
        content_type="tv",
        is_anime=True,
        poster_url="http://cdn.example.com/old.jpg",
    )
    db_session.add(work)
    await db_session.flush()
    candidate = _candidate(
        poster_url="http://cdn.example.com/new.jpg",
        metadata={"is_anime": False, "rating": "not-a-number", "number_of_episodes": "xyz"},
    )

    only_missing = await preview_work_metadata(
        db_session, work.id, "tv", candidate, False, only_missing=True
    )
    fields = {change["field"] for change in only_missing["changes"]}
    assert "poster_url" not in fields
    assert "is_anime" not in fields

    full = await preview_work_metadata(db_session, work.id, "tv", candidate, False)
    full_fields = {change["field"] for change in full["changes"]}
    assert "poster_url" in full_fields
    assert "is_anime" in full_fields
    # Badly typed numeric values are dropped by the safe casters.
    assert "rating" not in full_fields
    assert "number_of_episodes" not in full_fields


async def test_not_found_and_identity_conflict(db_session):
    with pytest.raises(HTTPException) as exc:
        await preview_work_metadata(db_session, _uuid(), "tv", _candidate(), False)
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        await apply_work_metadata(db_session, _uuid(), "tv", _candidate(), False)
    assert exc.value.status_code == 404

    owner = TVSeries(id=_uuid(), title_cn="Owner", content_type="tv")
    work = TVSeries(id=_uuid(), title_cn="Show", content_type="tv")
    db_session.add_all([owner, work])
    await db_session.flush()
    await add_external_id(db_session, "series", owner.id, "tmdb", "tmdb:999")
    with pytest.raises(HTTPException) as exc:
        await apply_work_metadata(
            db_session, work.id, "tv", _candidate(external_id="tmdb:999"), False
        )
    assert exc.value.status_code == 409


async def test_refresh_none_and_missing_title(db_session):
    result = await refresh_work_by_source(db_session, None, "tv", "tmdb")
    assert result["found"] is False
    assert "work not found" in result["message"]

    work = TVSeries(id=_uuid(), content_type="tv")
    db_session.add(work)
    await db_session.flush()
    with patch(_SEARCH, new_callable=AsyncMock) as search:
        result = await refresh_work_by_source(db_session, work, "tv", "tmdb")
    assert result["found"] is False
    assert "no title" in result["message"]
    search.assert_not_awaited()


async def test_bangumi_listing_and_expansion(db_session):
    subjects = [
        {
            "id": 101,
            "name": "Show",
            "name_cn": "番剧",
            "platform": "TV",
            "date": "2024-01-01",
        },
        {
            "id": 102,
            "name": "Movie",
            "name_cn": "剧场",
            "platform": "剧场版",
            "date": "2023-01-01",
        },
    ]
    request = MetadataSearchRequest(
        query="Show", content_type="tv", mode="online", source="bangumi"
    )
    with patch(_AVAILABLE, return_value=True), patch(
        "app.services.metadata_bangumi.list_bangumi_subjects",
        new_callable=AsyncMock,
        return_value=subjects,
    ):
        listing = await search_metadata_candidates(db_session, request, listing=True)
    assert [c.external_id for c in listing] == ["bangumi:101"]
    assert listing[0].identity_source == "bangumi"

    work = TVSeries(id=_uuid(), title_cn="番剧", content_type="tv", season_number=1)
    db_session.add(work)
    await db_session.flush()
    candidate = MetadataCandidate(
        origin="external",
        content_type="tv",
        title_cn="番剧",
        primary_source="bangumi",
        identity_source="bangumi",
        external_id="bangumi:101",
        match_path="primary",
        selectable=True,
        metadata={},
    )
    entity = {
        "title_cn": "番剧",
        "start_date": "2024-01-01",
        "rating": 8.0,
        "_content_type": "tv",
    }
    with patch(
        "app.services.metadata_bangumi.build_entity_for_subject",
        new_callable=AsyncMock,
        return_value=entity,
    ):
        preview = await preview_work_metadata(
            db_session, work.id, "tv", candidate, False
        )
    fields = {change["field"] for change in preview["changes"]}
    assert "start_date" in fields
    assert "rating" in fields


async def test_refresh_success_and_identity_conflict(db_session):
    work = TVSeries(id=_uuid(), title_en="Show", content_type="tv")
    db_session.add(work)
    await db_session.flush()
    hit = {
        "content_type": "tv",
        "title_en": "Show",
        "external_id": "tmdb:5",
        "external_source": "tmdb",
        "rating": 9.1,
    }
    with patch(_POSTER, new_callable=AsyncMock, return_value=None), patch(
        _AVAILABLE, return_value=True
    ), patch(_SEARCH, new_callable=AsyncMock, return_value=[hit]):
        result = await refresh_work_by_source(db_session, work, "tv", "tmdb")
    assert result["found"] is True
    assert "rating" in result["applied"]

    owner = TVSeries(id=_uuid(), title_cn="Owner", content_type="tv")
    other = TVSeries(id=_uuid(), title_en="Other", content_type="tv")
    db_session.add_all([owner, other])
    await db_session.flush()
    await add_external_id(db_session, "series", owner.id, "tmdb", "tmdb:6")
    conflict = {
        "content_type": "tv",
        "title_en": "Other",
        "external_id": "tmdb:6",
        "external_source": "tmdb",
    }
    with patch(_POSTER, new_callable=AsyncMock, return_value=None), patch(
        _AVAILABLE, return_value=True
    ), patch(_SEARCH, new_callable=AsyncMock, return_value=[conflict]):
        conflict_result = await refresh_work_by_source(
            db_session, other, "tv", "tmdb"
        )
    assert conflict_result["found"] is True
    assert conflict_result.get("identity_conflict") is True

