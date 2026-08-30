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

