"""Web-fallback refresh picks apply identity/links only (content stays
primary-source authoritative), and slug-form wikipedia ids converge with
pageid forms via MediaWiki resolution at bag-write time.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.models.movie import Movie
from app.models.series import TVSeries
from app.schemas.metadata_search import MetadataCandidate
from app.services.external_ids import find_work_by_external_id, list_external_ids
from app.services.metadata_search import apply_work_metadata, refresh_work_by_source

_SEARCH = "app.services.metadata_service.search_metadata_via_llm"
_POSTER = "app.services.metadata_search.download_and_cache_poster"
_AVAILABLE = "app.services.metadata_search.is_metadata_source_available"
_LANGLINK = "app.services.metadata_wikipedia_client._fetch_langlink_pageids"


def _uuid() -> str:
    return str(uuid.uuid4())


def _patch_search(candidates):
    return [
        patch(_SEARCH, new_callable=AsyncMock, return_value=candidates),
        patch(_POSTER, new_callable=AsyncMock, return_value=None),
        patch(_AVAILABLE, return_value=True),
    ]


async def test_refresh_web_fallback_pick_applies_identity_only(db_session):
    """The requested primary source (wikipedia) missed and the picked candidate
    resolved from a fallback site (tmdb): no content field may be written —
    only the identity enters the bag."""
    work = TVSeries(id=_uuid(), title_en="Fallback Show", content_type="tv", season_number=1)
    db_session.add(work)
    await db_session.flush()
    candidate = {
        "title_en": "Fallback Show",
        "content_type": "tv",
        "external_id": "tmdb:88",
        "external_source": "tmdb",
        "description": "fallback synopsis — must not be applied",
        "rating": 9.9,
        "start_date": "2020-01-01",
        "poster_url": "http://cdn.example.com/fallback.jpg",
        "is_anime": True,
        "episode_list": [{"season": 1, "episode": 1, "title": "E1"}],
    }
    patches = _patch_search([candidate])
    with patches[0], patches[1] as poster, patches[2]:
        result = await refresh_work_by_source(db_session, work, "tv", "wikipedia")

    assert result["found"] is True
    assert result["identity_only"] is True
    assert result["applied"] == []
    # Content untouched.
    assert work.description is None
    assert work.rating is None
    assert work.start_date is None
    assert work.poster_url is None
    assert work.is_anime is None
    assert work.external_id is None
    poster.assert_not_awaited()
    from sqlalchemy import select

    from app.models.episode import Episode

    assert (await db_session.execute(select(Episode))).scalars().all() == []
    # Identity bagged.
    owner = await find_work_by_external_id(db_session, "series", "tmdb", "tmdb:88")
    assert owner is not None and owner.id == work.id


async def test_refresh_primary_pick_still_applies_content(db_session):
    """Regression: a primary-source candidate (match_path='primary') keeps the
    full apply semantics."""
    work = TVSeries(id=_uuid(), title_en="Primary Show", content_type="tv", season_number=1)
    db_session.add(work)
    await db_session.flush()
    candidate = {
        "title_en": "Primary Show",
        "content_type": "tv",
        "external_id": "wikipedia:en:42",
        "external_source": "wikipedia",
        "description": "primary synopsis",
        "start_date": "2020-01-01",
    }
    patches = _patch_search([candidate])
    with patches[0], patches[1], patches[2]:
        result = await refresh_work_by_source(db_session, work, "tv", "wikipedia")
    assert "identity_only" not in result
    assert work.description == "primary synopsis"
    assert work.start_date == date(2020, 1, 1)


async def test_refresh_fallback_wikipedia_slug_fills_url_and_resolves_pageid(db_session):
    """A web-fallback wikipedia slug candidate (requested tmdb, fallback
    resolved a wikipedia page) fills the empty wikipedia_url (link) and bags
    the MediaWiki-resolved pageid next to the slug."""
    work = TVSeries(id=_uuid(), title_en="Slug Show", content_type="tv", season_number=1)
    db_session.add(work)
    await db_session.flush()
    candidate = {
        "title_en": "Slug Show",
        "content_type": "tv",
        "external_id": "wikipedia:Slug_Show",
        "external_source": "wikipedia",
        "url": "https://en.wikipedia.org/wiki/Slug_Show",
        "description": "must not be applied",
    }

    async def fake_resolve(langlinks):
        return {lang: 4242 for lang in langlinks if lang == "en"}

    patches = _patch_search([candidate])
    with patches[0], patches[1], patches[2], patch(_LANGLINK, side_effect=fake_resolve):
        result = await refresh_work_by_source(db_session, work, "tv", "tmdb")
    assert result["identity_only"] is True
    assert work.description is None
    assert work.wikipedia_url == "https://en.wikipedia.org/wiki/Slug_Show"
    assert "wikipedia_url" in result["applied"]
    bag = {(row.source, row.external_id) for row in await list_external_ids(db_session, "series", work.id)}
    assert ("wikipedia", "wikipedia:slug_show") in bag  # canonicalized slug
    assert ("wikipedia", "wikipedia:en:4242") in bag  # resolved pageid
    # Numeric-form lookups now converge on the same work.
    owner = await find_work_by_external_id(db_session, "series", "wikipedia", "wikipedia:4242")
    assert owner is not None and owner.id == work.id


async def test_apply_identity_only_rejects_identity_conflict(db_session):
    """The no-steal guards still run in identity-only mode."""
    owner = TVSeries(id=_uuid(), title_en="Owner", content_type="tv")
    work = TVSeries(id=_uuid(), title_en="Show", content_type="tv")
    db_session.add_all([owner, work])
    await db_session.flush()
    from app.services.external_ids import add_external_id

    await add_external_id(db_session, "series", owner.id, "tmdb", "tmdb:999")
    candidate = MetadataCandidate(
        origin="external", content_type="tv", title_en="Show",
        primary_source="wikipedia", identity_source="tmdb", external_id="tmdb:999",
        match_path="web_fallback", selectable=True, metadata={},
    )
    with pytest.raises(HTTPException) as exc:
        await apply_work_metadata(db_session, work.id, "tv", candidate, False, identity_only=True)
    assert exc.value.status_code == 409


async def test_apply_identity_only_respects_manual_wikipedia_url(db_session):
    work = TVSeries(
        id=_uuid(), title_en="Show", content_type="tv",
        wikipedia_url="https://zh.wikipedia.org/wiki/人工页面",
        manually_edited_fields=["wikipedia_url"],
    )
    db_session.add(work)
    await db_session.flush()
    candidate = MetadataCandidate(
        origin="external", content_type="tv", title_en="Show",
        primary_source="tmdb", identity_source="wikipedia",
        external_id="wikipedia:Some_Page",
        match_path="web_fallback", selectable=True,
        metadata={"url": "https://en.wikipedia.org/wiki/Some_Page"},
    )
    async def fake_resolve(langlinks):
        return {}

    with patch(_LANGLINK, side_effect=fake_resolve):
        result = await apply_work_metadata(
            db_session, work.id, "tv", candidate, False, identity_only=True,
        )
    assert result["applied"] == []
    assert work.wikipedia_url == "https://zh.wikipedia.org/wiki/人工页面"


# ---------------------------------------------------------------------------
# resolve_wikipedia_slug_id (metadata_service) + upsert bagging hook
# ---------------------------------------------------------------------------


async def test_resolve_wikipedia_slug_id_non_slug_never_hits_network(db_session):
    from app.services.metadata_service import resolve_wikipedia_slug_id

    with patch(_LANGLINK, new_callable=AsyncMock) as resolve:
        assert await resolve_wikipedia_slug_id("wikipedia:zh:7301786") is None
        assert await resolve_wikipedia_slug_id("wikipedia:7301786") is None
        assert await resolve_wikipedia_slug_id("tmdb:82684") is None
        assert await resolve_wikipedia_slug_id(None) is None
        resolve.assert_not_awaited()


async def test_resolve_wikipedia_slug_id_prefers_url_edition(db_session):
    from app.services.metadata_service import resolve_wikipedia_slug_id

    seen = {}

    async def fake_resolve(langlinks):
        seen.update(langlinks)
        # The slug exists on both editions as different pages; the URL's
        # edition (ja) must win over the zh default probe order.
        return {"zh": 111, "ja": 222}

    with patch(_LANGLINK, side_effect=fake_resolve):
        resolved = await resolve_wikipedia_slug_id(
            "wikipedia:Some_Title", "https://ja.wikipedia.org/wiki/Some_Title"
        )
    assert resolved == "wikipedia:ja:222"
    # zh/en/ja probes always run; the URL edition is included.
    assert set(seen) == {"ja", "zh", "en"}


async def test_resolve_wikipedia_slug_id_failure_returns_none(db_session):
    from app.services.metadata_service import resolve_wikipedia_slug_id

    async def fake_resolve(langlinks):
        return {}

    with patch(_LANGLINK, side_effect=fake_resolve):
        assert await resolve_wikipedia_slug_id("wikipedia:Missing_Page") is None


async def test_movie_upsert_bags_resolved_pageid_for_slug_id(db_session):
    """``_bag_matched_entity_ids`` resolves slug-form wikipedia primaries and
    bags the pageid form alongside the slug."""
    from app.services.metadata_service import _bag_matched_entity_ids

    movie = Movie(id=_uuid(), title_cn="电影", content_type="movie")
    db_session.add(movie)
    await db_session.flush()

    async def fake_resolve(langlinks):
        return {"zh": 777}

    with patch(_LANGLINK, side_effect=fake_resolve):
        await _bag_matched_entity_ids(db_session, "movie", movie.id, {
            "external_source": "wikipedia",
            "external_id": "wikipedia:葬送的芙莉蓮",
            "wikipedia_url": "https://zh.wikipedia.org/wiki/葬送的芙莉蓮",
        })
    bag = {(row.source, row.external_id) for row in await list_external_ids(db_session, "movie", movie.id)}
    assert ("wikipedia", "wikipedia:葬送的芙莉蓮") in bag
    assert ("wikipedia", "wikipedia:zh:777") in bag
    owner = await find_work_by_external_id(db_session, "movie", "wikipedia", "wikipedia:zh:777")
    assert owner is not None and owner.id == movie.id


async def test_series_upsert_bags_resolved_pageid_for_slug_id(db_session):
    """The series-side granularity bagging (`_bag_entity_ids_by_granularity`)
    resolves slugs too; wikipedia ids are series-granular and land on the
    collection bag."""
    from app.models.work_collection import WorkCollection
    from app.services.metadata_service import _bag_entity_ids_by_granularity

    collection = WorkCollection(id=_uuid(), title_cn="合集", external_source="series_group")
    work = TVSeries(
        id=_uuid(), title_cn="剧", content_type="tv", season_number=1,
        collection_id=collection.id,
    )
    db_session.add_all([collection, work])
    await db_session.flush()

    async def fake_resolve(langlinks):
        return {"zh": 888}

    with patch(_LANGLINK, side_effect=fake_resolve):
        await _bag_entity_ids_by_granularity(
            db_session,
            work=work,
            collection=collection,
            data={
                "external_source": "wikipedia",
                "external_id": "wikipedia:某作品",
                "wikipedia_url": "https://zh.wikipedia.org/wiki/某作品",
            },
            series_level_id=None,
        )
    coll_bag = {
        (row.source, row.external_id)
        for row in await list_external_ids(db_session, "collection", collection.id)
    }
    assert ("wikipedia", "wikipedia:某作品") in coll_bag
    assert ("wikipedia", "wikipedia:zh:888") in coll_bag
