"""API tests for works endpoints (metadata config + refresh actions)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


class TestWorksMetadataConfig:
    async def test_get_config_initial(self, client):
        res = await client.get("/api/v1/works/metadata-config")
        assert res.status_code == 200
        data = res.json()["data"]
        assert "sources" in data
        assert data["default_source"] is None
        assert data["auto_refresh_enabled"] is False
        assert data["auto_refresh_interval_minutes"] == 1440

    async def test_put_then_get_config(self, client):
        res = await client.put(
            "/api/v1/works/metadata-config",
            json={
                "default_source": "wikipedia",
                "auto_refresh_enabled": True,
                "auto_refresh_interval_minutes": 60,
            },
        )
        assert res.status_code == 200
        assert res.json()["data"]["default_source"] == "wikipedia"
        got = await client.get("/api/v1/works/metadata-config")
        data = got.json()["data"]
        assert data["default_source"] == "wikipedia"
        assert data["auto_refresh_enabled"] is True
        assert data["auto_refresh_interval_minutes"] == 60

    async def test_put_rejects_invalid_source(self, client):
        res = await client.put("/api/v1/works/metadata-config", json={"default_source": "bogus"})
        assert res.status_code == 422

    async def test_put_rejects_empty_source(self, client):
        res = await client.put("/api/v1/works/metadata-config", json={"default_source": None})
        assert res.status_code == 422


class TestWorksRefreshMetadata:
    async def test_refresh_single_fills_missing(self, client, sample_series):
        candidate = {
            "content_type": "tv",
            "title_cn": "测试剧集",
            "title_en": "Test Series",
            "poster_url": "https://example.com/p.jpg",
            "rating": 9.0,
            "description": "A test series.",
        }
        with patch(
            "app.services.metadata_service.search_metadata_via_llm",
            AsyncMock(return_value=[candidate]),
        ), patch(
            "app.services.metadata_service.download_and_cache_poster",
            AsyncMock(return_value="/posters/cached.jpg"),
        ):
            res = await client.post(
                "/api/v1/works/refresh-metadata",
                json={"id": sample_series.id, "content_type": "tv", "source": "wikipedia"},
            )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["found"] is True
        assert "poster_url" in data["filled"]
        assert "rating" in data["filled"]
        # The persisted series now has the cached poster.
        got = await client.get(f"/api/v1/series/{sample_series.id}")
        assert got.json()["data"]["poster_url"] == "/posters/cached.jpg"
        assert got.json()["data"]["rating"] == 9.0

    async def test_refresh_single_not_found(self, client):
        with patch(
            "app.services.metadata_service.search_metadata_via_llm",
            AsyncMock(return_value=[]),
        ):
            res = await client.post(
                "/api/v1/works/refresh-metadata",
                json={"id": "nope", "content_type": "tv", "source": "wikipedia"},
            )
        assert res.status_code == 200
        assert res.json()["data"]["found"] is False

    async def test_refresh_uses_configured_default_source(self, client, sample_series):
        """When no source is passed, the configured default is used."""
        await client.put("/api/v1/works/metadata-config", json={"default_source": "wikipedia"})
        with patch(
            "app.services.metadata_service.search_metadata_via_llm",
            AsyncMock(return_value=[]),
        ) as mocked:
            await client.post(
                "/api/v1/works/refresh-metadata",
                json={"id": sample_series.id, "content_type": "tv"},
            )
        # search_metadata_via_llm(title, source) — source should be wikipedia.
        assert mocked.call_args.args[1] == "wikipedia"

    async def test_batch_refresh_enqueues_job(self, client, sample_series, monkeypatch):
        from app.services import task_queue as tq_mod

        fake = MagicMock()
        fake.enqueue = AsyncMock(return_value={"job_id": "j1", "status": "queued"})
        monkeypatch.setattr(tq_mod, "task_queue", fake)
        res = await client.post(
            "/api/v1/works/batch-refresh-metadata",
            json={
                "items": [{"id": sample_series.id, "content_type": "tv"}],
                "source": "wikipedia",
            },
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["count"] == 1
        assert data["job"]["job_id"] == "j1"
        fake.enqueue.assert_awaited_once()
        assert fake.enqueue.call_args.args[0] == "refresh_works_metadata"
        payload = fake.enqueue.call_args.args[2]
        assert payload["source"] == "wikipedia"
        assert payload["items"][0]["id"] == sample_series.id

    async def test_batch_refresh_empty(self, client):
        res = await client.post(
            "/api/v1/works/batch-refresh-metadata",
            json={"items": []},
        )
        assert res.status_code == 200
        assert res.json()["data"]["count"] == 0
        assert res.json()["data"]["job"] is None
