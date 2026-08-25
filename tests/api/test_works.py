"""API tests for works endpoints (metadata config + refresh actions)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.audio_work import AudioWork
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection


def _uuid() -> str:
    return str(uuid.uuid4())


class TestWorksMetadataConfig:
    async def test_get_config_returns_catalog(self, client):
        """The config endpoint is now a source catalog only: there is no
        global default source and no global auto-refresh toggle."""
        res = await client.get("/api/v1/works/metadata-config")
        assert res.status_code == 200
        data = res.json()["data"]
        assert "sources" in data
        assert set(data.keys()) == {"sources"}


class TestWorksRefreshMetadata:
    async def test_refresh_requires_source(self, client, sample_series):
        """No source → 422 (source is a required field; there is no global
        default to fall back to)."""
        res = await client.post(
            "/api/v1/works/refresh-metadata",
            json={"id": sample_series.id, "content_type": "tv"},
        )
        assert res.status_code == 422

    async def test_batch_refresh_requires_source(self, client, sample_series):
        res = await client.post(
            "/api/v1/works/batch-refresh-metadata",
            json={"items": [{"id": sample_series.id, "content_type": "tv"}]},
        )
        assert res.status_code == 422

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

    async def test_refresh_skips_manual_edits_unless_overridden(self, client, sample_series):
        """Manual edits are kept by default; override_manual_edits fills them."""
        candidate = {
            "content_type": "tv",
            "title_cn": "测试剧集",
            "title_en": "Test Series",
            "rating": 9.0,
            "description": "A test series.",
        }
        # Explicitly clearing the fields marks them manually-edited and empties
        # them, so a default refresh would otherwise re-fill them.
        await client.put(
            f"/api/v1/series/{sample_series.id}",
            json={"rating": None, "description": None},
        )
        with patch(
            "app.services.metadata_service.search_metadata_via_llm",
            AsyncMock(return_value=[candidate]),
        ), patch(
            "app.services.metadata_service.download_and_cache_poster",
            AsyncMock(return_value=None),
        ):
            res = await client.post(
                "/api/v1/works/refresh-metadata",
                json={"id": sample_series.id, "content_type": "tv", "source": "wikipedia"},
            )
        assert res.status_code == 200
        filled = res.json()["data"]["filled"]
        assert "rating" not in filled and "description" not in filled
        got = await client.get(f"/api/v1/series/{sample_series.id}")
        assert got.json()["data"]["rating"] is None
        assert got.json()["data"]["description"] is None

        with patch(
            "app.services.metadata_service.search_metadata_via_llm",
            AsyncMock(return_value=[candidate]),
        ), patch(
            "app.services.metadata_service.download_and_cache_poster",
            AsyncMock(return_value=None),
        ):
            res2 = await client.post(
                "/api/v1/works/refresh-metadata",
                json={
                    "id": sample_series.id,
                    "content_type": "tv",
                    "source": "wikipedia",
                    "override_manual_edits": True,
                },
            )
        assert res2.status_code == 200
        filled2 = res2.json()["data"]["filled"]
        assert "rating" in filled2 and "description" in filled2
        got2 = await client.get(f"/api/v1/series/{sample_series.id}")
        assert got2.json()["data"]["rating"] == 9.0
        assert got2.json()["data"]["description"] == "A test series."

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
            json={"items": [], "source": "wikipedia"},
        )
        assert res.status_code == 200
        assert res.json()["data"]["count"] == 0
        assert res.json()["data"]["job"] is None


class TestWorksListCollectionFilter:
    async def _seed(self, db_session):
        coll = WorkCollection(id=_uuid(), title_cn="攻壳机动队（系列）", title_en="Ghost in the Shell")
        grouped_series = TVSeries(
            id=_uuid(), title_cn="攻壳机动队 SAC", content_type="tv", collection_id=coll.id
        )
        grouped_movie = Movie(
            id=_uuid(), title_cn="攻壳机动队", content_type="movie", collection_id=coll.id
        )
        free_series = TVSeries(id=_uuid(), title_cn="独立剧集", content_type="tv")
        audio = AudioWork(id=_uuid(), title_cn="测试音频", content_type="asmr")
        db_session.add_all([coll, grouped_series, grouped_movie, free_series, audio])
        await db_session.commit()
        return coll, grouped_series, grouped_movie, free_series, audio

    async def test_list_includes_collection_fields(self, client, db_session):
        coll, grouped_series, _, free_series, audio = await self._seed(db_session)
        res = await client.get("/api/v1/works")
        assert res.status_code == 200
        items = {w["id"]: w for w in res.json()["data"]}
        assert items[grouped_series.id]["collection_id"] == coll.id
        assert items[grouped_series.id]["collection_name"] == "攻壳机动队（系列）"
        assert items[free_series.id]["collection_id"] is None
        assert items[free_series.id]["collection_name"] is None
        # AudioWork has no collection — fields are None/absent-equivalent.
        assert items[audio.id].get("collection_id") is None
        assert items[audio.id].get("collection_name") is None

    async def test_filter_by_collection_id(self, client, db_session):
        coll, grouped_series, grouped_movie, free_series, audio = await self._seed(db_session)
        res = await client.get("/api/v1/works", params={"collection_id": coll.id})
        assert res.status_code == 200
        ids = {w["id"] for w in res.json()["data"]}
        assert ids == {grouped_series.id, grouped_movie.id}

        # Combines with content_type.
        res = await client.get(
            "/api/v1/works", params={"collection_id": coll.id, "content_type": "movie"}
        )
        assert {w["id"] for w in res.json()["data"]} == {grouped_movie.id}

    async def test_filter_collection_none(self, client, db_session):
        _, grouped_series, grouped_movie, free_series, audio = await self._seed(db_session)
        res = await client.get("/api/v1/works", params={"collection_id": "none"})
        assert res.status_code == 200
        ids = {w["id"] for w in res.json()["data"]}
        # Audio excluded when collection_id is present; grouped works filtered out.
        assert ids == {free_series.id}
        assert grouped_series.id not in ids and grouped_movie.id not in ids
        assert audio.id not in ids
