"""API tests for works endpoints (metadata config + refresh actions)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.models.audio_work import AudioWork
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection


def _uuid() -> str:
    return str(uuid.uuid4())


class TestRemovedWorksMetadataEndpoints:
    async def test_refresh_requires_source(self, client, sample_series):
        """The blind-refresh endpoint was replaced by search/preview/apply."""
        res = await client.post(
            "/api/v1/works/refresh-metadata",
            json={"id": sample_series.id, "content_type": "tv"},
        )
        assert res.status_code == 404

    async def test_batch_refresh_requires_source(self, client, sample_series):
        res = await client.post(
            "/api/v1/works/batch-refresh-metadata",
            json={"items": [{"id": sample_series.id, "content_type": "tv"}]},
        )
        assert res.status_code == 422

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
