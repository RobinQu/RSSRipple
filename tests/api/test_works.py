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


class TestWorksMerge:
    """POST /works/merge — manual same-season merge entry (per-season works)."""

    async def _make_series(
        self, db_session, *, title="无职转生", season_number=1, created_at=None
    ):
        s = TVSeries(
            id=_uuid(), title_cn=title, title_en="Mushoku Tensei",
            content_type="tv", season_number=season_number,
        )
        if created_at is not None:
            s.created_at = created_at
        db_session.add(s)
        await db_session.commit()
        return s

    async def test_merge_requires_confirm(self, client, db_session):
        s1 = await self._make_series(db_session)
        s2 = await self._make_series(db_session)
        res = await client.post(
            "/api/v1/works/merge",
            json={
                "survivor_type": "series",
                "survivor_id": s1.id,
                "duplicate_ids": [s2.id],
            },
        )
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
        # Nothing merged.
        from sqlalchemy import select

        remaining = (await db_session.execute(select(TVSeries))).scalars().all()
        assert len(remaining) == 2

    async def test_merge_rejects_cross_season(self, client, db_session):
        s1 = await self._make_series(db_session, season_number=1)
        s3 = await self._make_series(db_session, season_number=3)
        res = await client.post(
            "/api/v1/works/merge",
            json={
                "survivor_type": "series",
                "survivor_id": s1.id,
                "duplicate_ids": [s3.id],
                "confirm": True,
            },
        )
        assert res.status_code == 422
        assert "同季" in res.json()["error"]["message"]

    async def test_merge_unknown_work_404(self, client, db_session):
        s1 = await self._make_series(db_session)
        res = await client.post(
            "/api/v1/works/merge",
            json={
                "survivor_type": "series",
                "survivor_id": s1.id,
                "duplicate_ids": [_uuid()],
                "confirm": True,
            },
        )
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "NOT_FOUND"

    async def test_merge_survivor_in_duplicates_rejected(self, client, db_session):
        s1 = await self._make_series(db_session)
        res = await client.post(
            "/api/v1/works/merge",
            json={
                "survivor_type": "series",
                "survivor_id": s1.id,
                "duplicate_ids": [s1.id],
                "confirm": True,
            },
        )
        assert res.status_code == 422

    async def test_merge_series_success_repoints_children(
        self, client, db_session, sample_channel
    ):
        from sqlalchemy import select

        from app.models.file_resource import FileResource

        s1 = await self._make_series(db_session, title="无职转生 第三季")
        s2 = await self._make_series(db_session, title="无职转生 第三季")
        res_row = FileResource(
            id=_uuid(), channel_id=sample_channel.id, guid="g-merge",
            title_raw="raw", torrent_url="magnet:?xt=1", series_id=s2.id,
        )
        db_session.add(res_row)
        await db_session.commit()

        res = await client.post(
            "/api/v1/works/merge",
            json={
                "survivor_type": "series",
                "survivor_id": s1.id,
                "duplicate_ids": [s2.id],
                "confirm": True,
            },
        )
        assert res.status_code == 200, res.text[:500]
        data = res.json()["data"]
        assert data["survivor_id"] == s1.id
        assert data["merged"] == 1
        assert data["file_resources_updated"] == 1

        remaining = (await db_session.execute(select(TVSeries))).scalars().all()
        assert [s.id for s in remaining] == [s1.id]
        await db_session.refresh(res_row)
        assert res_row.series_id == s1.id

    async def test_merge_movies_success(self, client, db_session):
        from sqlalchemy import select

        m1 = Movie(id=_uuid(), title_cn="攻壳机动队", content_type="movie")
        m2 = Movie(id=_uuid(), title_cn="攻壳机动队", content_type="movie")
        db_session.add_all([m1, m2])
        await db_session.commit()
        res = await client.post(
            "/api/v1/works/merge",
            json={
                "survivor_type": "movie",
                "survivor_id": m2.id,  # explicit survivor wins over age order
                "duplicate_ids": [m1.id],
                "confirm": True,
            },
        )
        assert res.status_code == 200, res.text[:500]
        assert res.json()["data"]["survivor_id"] == m2.id
        remaining = (await db_session.execute(select(Movie))).scalars().all()
        assert [m.id for m in remaining] == [m2.id]
