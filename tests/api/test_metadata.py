"""Unified metadata search/preview/apply API tests."""

from unittest.mock import AsyncMock, patch


async def test_source_catalog_has_one_canonical_primary_set(client):
    response = await client.get("/api/v1/metadata/sources")
    assert response.status_code == 200
    data = response.json()["data"]
    assert {item["value"] for item in data["primary_sources"]} == {
        "wikipedia", "tmdb", "bangumi",
    }
    assert "jina" not in str(data).lower()
    assert {item["value"] for item in data["trusted_sites"]} == {
        "wikipedia", "tmdb", "bangumi", "mal", "anilist", "imdb", "douban",
    }


async def test_search_forwards_ordered_trusted_sites(client, monkeypatch):
    monkeypatch.setattr(
        "app.services.metadata_search.is_metadata_source_available", lambda _: True
    )
    candidate = {
        "content_type": "tv",
        "title_cn": "候选",
        "external_source": "bangumi",
        "external_id": "bangumi:1",
    }
    with patch(
        "app.services.metadata_search.manual_search_metadata",
        AsyncMock(return_value=[candidate]),
    ) as search:
        response = await client.post("/api/v1/metadata/search", json={
            "query": "候选", "content_type": "tv", "mode": "online",
            "source": "wikipedia", "trusted_sites": ["bangumi", "anilist"],
        })
    assert response.status_code == 200
    search.assert_awaited_once()
    assert search.await_args.args[-1] == ["bangumi", "anilist"]
    result = response.json()["data"]["candidates"][0]
    assert result["match_path"] == "web_fallback"
    assert result["selectable"] is True


async def test_search_rejects_unknown_trusted_site(client):
    response = await client.post("/api/v1/metadata/search", json={
        "query": "x", "content_type": "tv", "mode": "online",
        "source": "wikipedia", "trusted_sites": ["example"],
    })
    assert response.status_code == 422


async def test_local_search_rejects_external_options(client):
    response = await client.post("/api/v1/metadata/search", json={
        "query": "x", "content_type": "tv", "mode": "local",
        "source": "wikipedia",
    })
    assert response.status_code == 422


async def test_work_preview_and_apply_protect_manual_fields(client, sample_series):
    await client.put(f"/api/v1/series/{sample_series.id}", json={"description": "人工简介"})
    candidate = {
        "origin": "external", "content_type": "tv", "title_cn": "测试剧集",
        "title_en": "Test", "original_title": "Test", "year": 2024,
        "poster_url": None, "work_id": None, "primary_source": "wikipedia",
        "identity_source": "wikipedia", "external_id": "wikipedia:en:1",
        "match_path": "primary", "selectable": True, "unavailable_reason": None,
        "metadata": {"description": "来源简介", "external_source": "wikipedia",
                     "external_id": "wikipedia:en:1"},
    }
    body = {"id": sample_series.id, "content_type": "tv", "candidate": candidate,
            "override_manual_edits": False}
    preview = await client.post("/api/v1/works/metadata/preview", json=body)
    assert preview.status_code == 200
    description = next(c for c in preview.json()["data"]["changes"] if c["field"] == "description")
    assert description["action"] == "skip"

    with patch(
        "app.services.metadata_search.download_and_cache_poster",
        AsyncMock(return_value=None),
    ):
        applied = await client.post("/api/v1/works/metadata/apply", json=body)
    assert applied.status_code == 200
    assert "description" in applied.json()["data"]["skipped"]



# ---------------------------------------------------------------------------
# Bangumi listing mode (POST /metadata/search, source=bangumi)
# ---------------------------------------------------------------------------


def _bangumi_subjects():
    return [
        {"id": 1, "name": "頭文字D First Stage", "name_cn": "头文字D",
         "date": "1998-04-18", "platform": "TV",
         "images": {"large": "https://img/1.jpg"}, "rating": {"score": 8.2},
         "summary": "first stage"},
        {"id": 2, "name": "頭文字D Second Stage", "name_cn": "头文字D Second Stage",
         "date": "1999-10-15", "platform": "TV", "summary": "second stage"},
        {"id": 3, "name": "頭文字D Third Stage", "name_cn": "头文字D Third Stage",
         "date": "2001-01-13", "platform": "剧场版", "summary": "third stage"},
    ]


async def test_bangumi_search_listing_mode_returns_all_hits(client, monkeypatch):
    monkeypatch.setattr(
        "app.services.metadata_search.is_metadata_source_available", lambda _: True
    )
    monkeypatch.setattr(
        "app.services.metadata_bangumi.list_bangumi_subjects",
        AsyncMock(return_value=_bangumi_subjects()),
    )
    with patch(
        "app.services.metadata_search.manual_search_metadata",
        AsyncMock(side_effect=AssertionError("LLM pipeline must not run in listing mode")),
    ) as llm:
        response = await client.post("/api/v1/metadata/search", json={
            "query": "头文字D", "content_type": "tv", "mode": "online",
            "source": "bangumi",
        })
    assert response.status_code == 200
    llm.assert_not_awaited()
    candidates = response.json()["data"]["candidates"]
    # The movie-platform subject is filtered out of a tv search; both TV hits
    # come back as selectable candidates with full identities — no convergence.
    assert [c["external_id"] for c in candidates] == ["bangumi:1", "bangumi:2"]
    first, second = candidates
    assert first["selectable"] is True
    assert first["identity_source"] == "bangumi"
    assert first["match_path"] == "primary"
    assert first["title_cn"] == "头文字D"
    assert first["original_title"] == "頭文字D First Stage"
    assert first["year"] == 1998
    assert first["poster_url"] == "https://img/1.jpg"
    # P1 Stage ordinals: "First Stage"/"Second Stage" carry their markers.
    assert first["metadata"]["subject_season"] == 1
    assert second["metadata"]["subject_season"] == 2


async def test_bangumi_search_listing_mode_movie_search(client, monkeypatch):
    monkeypatch.setattr(
        "app.services.metadata_search.is_metadata_source_available", lambda _: True
    )
    monkeypatch.setattr(
        "app.services.metadata_bangumi.list_bangumi_subjects",
        AsyncMock(return_value=_bangumi_subjects()),
    )
    response = await client.post("/api/v1/metadata/search", json={
        "query": "头文字D", "content_type": "movie", "mode": "online",
        "source": "bangumi",
    })
    assert response.status_code == 200
    candidates = response.json()["data"]["candidates"]
    assert [c["external_id"] for c in candidates] == ["bangumi:3"]
    assert candidates[0]["content_type"] == "movie"


def _thin_bangumi_candidate():
    return {
        "origin": "external", "content_type": "tv", "title_cn": "头文字D",
        "original_title": "頭文字D Second Stage", "year": 1999,
        "poster_url": None, "work_id": None, "primary_source": "bangumi",
        "identity_source": "bangumi", "external_id": "bangumi:7",
        "match_path": "primary", "selectable": True, "unavailable_reason": None,
        # Listing-mode metadata: search-response fields only, no details.
        "metadata": {"start_date": "1999-10-15", "subject_season": 2},
    }


def _full_bangumi_entity():
    return {
        "external_id": "bangumi:7", "external_source": "bangumi",
        "title_cn": "头文字D", "original_title": "頭文字D Second Stage",
        "description": "详情简介", "rating": 8.6,
        "start_date": "1999-10-15", "number_of_episodes": 13,
        "genre": ["Action"], "is_anime": True,
        "episode_list": [
            {"season": 1, "episode": 1, "title": "ep1", "air_date": "1999-10-15"},
            {"season": 1, "episode": 2, "title": "ep2", "air_date": "1999-10-22"},
        ],
        "_content_type": "tv", "single_season_entry": True,
    }


async def test_bangumi_candidate_preview_expands_on_demand(
    client, sample_series, monkeypatch
):
    expand = AsyncMock(return_value=_full_bangumi_entity())
    monkeypatch.setattr(
        "app.services.metadata_bangumi.build_entity_for_subject", expand
    )
    body = {
        "id": sample_series.id, "content_type": "tv",
        "candidate": _thin_bangumi_candidate(), "override_manual_edits": False,
    }
    preview = await client.post("/api/v1/works/metadata/preview", json=body)
    assert preview.status_code == 200
    expand.assert_awaited_once_with(7, season=1)
    fields = {c["field"] for c in preview.json()["data"]["changes"]}
    # Detail-only fields surface in the diff after expansion.
    assert {"number_of_episodes", "description", "genre"} <= fields


async def test_bangumi_candidate_apply_expands_once_and_upserts_episodes(
    client, sample_series, db_session, monkeypatch
):
    expand = AsyncMock(return_value=_full_bangumi_entity())
    monkeypatch.setattr(
        "app.services.metadata_bangumi.build_entity_for_subject", expand
    )
    body = {
        "id": sample_series.id, "content_type": "tv",
        "candidate": _thin_bangumi_candidate(), "override_manual_edits": False,
    }
    with patch(
        "app.services.metadata_search.download_and_cache_poster",
        AsyncMock(return_value=None),
    ):
        applied = await client.post("/api/v1/works/metadata/apply", json=body)
    assert applied.status_code == 200
    # Apply expands once and passes the values through to its internal preview.
    assert expand.await_count == 1
    assert "number_of_episodes" in applied.json()["data"]["applied"]

    from sqlalchemy import select

    from app.models.episode import Episode

    episodes = (await db_session.execute(
        select(Episode).where(Episode.series_id == sample_series.id)
    )).scalars().all()
    assert {e.episode for e in episodes} == {1, 2}

    # The bangumi identity entered the work's identity bag.
    from app.models.work_external_id import WorkExternalId

    bag = (await db_session.execute(
        select(WorkExternalId).where(
            WorkExternalId.work_type == "series",
            WorkExternalId.work_id == sample_series.id,
            WorkExternalId.source == "bangumi",
        )
    )).scalars().all()
    assert [row.external_id for row in bag] == ["bangumi:7"]


async def test_bangumi_candidate_preview_keeps_thin_values_on_expansion_failure(
    client, sample_series, monkeypatch
):
    monkeypatch.setattr(
        "app.services.metadata_bangumi.build_entity_for_subject",
        AsyncMock(return_value=None),
    )
    body = {
        "id": sample_series.id, "content_type": "tv",
        "candidate": _thin_bangumi_candidate(), "override_manual_edits": False,
    }
    preview = await client.post("/api/v1/works/metadata/preview", json=body)
    assert preview.status_code == 200
    fields = {c["field"] for c in preview.json()["data"]["changes"]}
    # Only the thin candidate fields diff; no detail fields appear.
    assert "start_date" in fields
    assert "number_of_episodes" not in fields
