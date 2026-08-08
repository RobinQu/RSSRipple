"""API tests for WorkCollection CRUD-lite + work attach/detach + detail siblings."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection


def _uuid() -> str:
    return str(uuid.uuid4())


class TestCollectionsCRUD:
    async def test_create_collection(self, client):
        res = await client.post("/api/v1/collections", json={
            "title_cn": "狮子王（系列）", "description": "desc",
        })
        assert res.status_code == 201
        data = res.json()["data"]
        assert data["title_cn"] == "狮子王（系列）"
        assert data["description"] == "desc"

    async def test_list_collections_paginated(self, client):
        await client.post("/api/v1/collections", json={"title_cn": "攻壳机动队（系列）"})
        await client.post("/api/v1/collections", json={"title_cn": "蜘蛛侠（系列）"})
        res = await client.get("/api/v1/collections", params={"page": 1, "page_size": 1})
        assert res.status_code == 200
        body = res.json()
        assert body["meta"]["total"] == 2
        assert len(body["data"]) == 1

        res = await client.get("/api/v1/collections", params={"search": "攻壳"})
        assert res.json()["meta"]["total"] == 1

    async def test_get_collection_includes_works(self, client, sample_movie):
        cid = (await client.post(
            "/api/v1/collections", json={"title_cn": "测试系列"}
        )).json()["data"]["id"]
        await client.post(f"/api/v1/collections/{cid}/works", json={
            "work_type": "movie", "work_id": sample_movie.id,
        })
        res = await client.get(f"/api/v1/collections/{cid}")
        assert res.status_code == 200
        works = res.json()["data"]["works"]
        assert [w["id"] for w in works] == [sample_movie.id]
        assert works[0]["type"] == "movie"

    async def test_get_404(self, client):
        res = await client.get("/api/v1/collections/nope")
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "NOT_FOUND"

    async def test_patch_collection(self, client):
        cid = (await client.post(
            "/api/v1/collections", json={"title_cn": "旧名"}
        )).json()["data"]["id"]
        res = await client.patch(f"/api/v1/collections/{cid}", json={
            "title_cn": "新名", "description": "新描述",
        })
        assert res.status_code == 200
        assert res.json()["data"]["title_cn"] == "新名"
        assert res.json()["data"]["description"] == "新描述"

    async def test_delete_detaches_member_works(self, client, db_session, sample_series):
        cid = (await client.post(
            "/api/v1/collections", json={"title_cn": "X"}
        )).json()["data"]["id"]
        await client.post(f"/api/v1/collections/{cid}/works", json={
            "work_type": "series", "work_id": sample_series.id,
        })
        res = await client.delete(f"/api/v1/collections/{cid}")
        assert res.status_code == 200
        assert (await client.get(f"/api/v1/collections/{cid}")).status_code == 404
        await db_session.refresh(sample_series)
        assert sample_series.collection_id is None


class TestCollectionAttach:
    async def test_attach_and_detach_series(self, client, db_session, sample_series):
        cid = (await client.post(
            "/api/v1/collections", json={"title_cn": "攻壳机动队（系列）"}
        )).json()["data"]["id"]
        res = await client.post(f"/api/v1/collections/{cid}/works", json={
            "work_type": "series", "work_id": sample_series.id,
        })
        assert res.status_code == 201
        await db_session.refresh(sample_series)
        assert sample_series.collection_id == cid

        res = await client.delete(
            f"/api/v1/collections/{cid}/works/{sample_series.id}",
            params={"work_type": "series"},
        )
        assert res.status_code == 200
        await db_session.refresh(sample_series)
        assert sample_series.collection_id is None

    async def test_attach_occupied_work_returns_409(self, client, sample_movie):
        c1 = (await client.post(
            "/api/v1/collections", json={"title_cn": "A"}
        )).json()["data"]["id"]
        c2 = (await client.post(
            "/api/v1/collections", json={"title_cn": "B"}
        )).json()["data"]["id"]
        await client.post(f"/api/v1/collections/{c1}/works", json={
            "work_type": "movie", "work_id": sample_movie.id,
        })
        res = await client.post(f"/api/v1/collections/{c2}/works", json={
            "work_type": "movie", "work_id": sample_movie.id,
        })
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "DUPLICATE_SUBMISSION"

    async def test_reattach_same_collection_is_idempotent(self, client, sample_movie):
        cid = (await client.post(
            "/api/v1/collections", json={"title_cn": "A"}
        )).json()["data"]["id"]
        body = {"work_type": "movie", "work_id": sample_movie.id}
        assert (await client.post(f"/api/v1/collections/{cid}/works", json=body)).status_code == 201
        assert (await client.post(f"/api/v1/collections/{cid}/works", json=body)).status_code == 201

    async def test_attach_invalid_work_type_422(self, client):
        cid = (await client.post(
            "/api/v1/collections", json={"title_cn": "A"}
        )).json()["data"]["id"]
        res = await client.post(f"/api/v1/collections/{cid}/works", json={
            "work_type": "audio", "work_id": "x",
        })
        assert res.status_code == 422

    async def test_attach_missing_work_404(self, client):
        cid = (await client.post(
            "/api/v1/collections", json={"title_cn": "A"}
        )).json()["data"]["id"]
        res = await client.post(f"/api/v1/collections/{cid}/works", json={
            "work_type": "movie", "work_id": "nope",
        })
        assert res.status_code == 404

    async def test_detach_not_member_404(self, client, sample_series):
        cid = (await client.post(
            "/api/v1/collections", json={"title_cn": "A"}
        )).json()["data"]["id"]
        res = await client.delete(
            f"/api/v1/collections/{cid}/works/{sample_series.id}",
            params={"work_type": "series"},
        )
        assert res.status_code == 404


class TestDetailCollectionFields:
    async def test_movie_detail_includes_collection_and_siblings(self, client, db_session):
        coll = WorkCollection(id=_uuid(), title_cn="狮子王（系列）")
        m1 = Movie(id=_uuid(), title_cn="狮子王", content_type="movie", collection_id=coll.id)
        m2 = Movie(id=_uuid(), title_cn="狮子王2", content_type="movie", collection_id=coll.id)
        db_session.add_all([coll, m1, m2])
        await db_session.commit()

        res = await client.get(f"/api/v1/movies/{m1.id}")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["collection"] == {"id": coll.id, "name": "狮子王（系列）"}
        assert [s["id"] for s in data["collection_siblings"]] == [m2.id]
        assert data["collection_siblings"][0]["type"] == "movie"

    async def test_series_detail_includes_collection_and_siblings(self, client, db_session):
        coll = WorkCollection(id=_uuid(), title_cn="攻壳机动队（系列）")
        s = TVSeries(id=_uuid(), title_cn="攻壳机动队 SAC", content_type="tv", collection_id=coll.id)
        m = Movie(id=_uuid(), title_cn="攻壳机动队", content_type="movie", collection_id=coll.id)
        db_session.add_all([coll, s, m])
        await db_session.commit()

        res = await client.get(f"/api/v1/series/{s.id}")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["collection"]["id"] == coll.id
        siblings = data["collection_siblings"]
        assert [w["id"] for w in siblings] == [m.id]
        assert siblings[0]["type"] == "movie"

    async def test_detail_without_collection(self, client, sample_movie):
        res = await client.get(f"/api/v1/movies/{sample_movie.id}")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["collection"] is None
        assert data["collection_siblings"] == []


class TestCollectionIncludeParts:
    async def test_include_parts_returns_untracked_only(self, client, db_session):
        coll = WorkCollection(
            id=_uuid(),
            title_cn="狮子王（系列）",
            external_source="tmdb_collection",
            external_id="131295",
        )
        tracked = Movie(
            id=_uuid(),
            title_cn="狮子王",
            content_type="movie",
            external_id="tmdb:1327",
            collection_id=coll.id,
        )
        db_session.add_all([coll, tracked])
        await db_session.commit()

        parts = [
            {"tmdb_id": "1327", "title": "狮子王", "year": 1994, "poster_url": None},
            {"tmdb_id": "999999", "title": "狮子王 2026", "year": 2026, "poster_url": "https://x/p.jpg"},
        ]
        with patch(
            "app.api.v1.collections.fetch_tmdb_collection_parts",
            new=AsyncMock(return_value=parts),
        ):
            res = await client.get(
                f"/api/v1/collections/{coll.id}", params={"include_parts": "true"}
            )
        assert res.status_code == 200
        data = res.json()["data"]
        # Tracked part excluded (already in `works`); untracked one surfaced.
        assert [p["tmdb_id"] for p in data["untracked_parts"]] == ["999999"]
        assert data["untracked_parts"][0]["year"] == 2026
        assert [w["id"] for w in data["works"]] == [tracked.id]

    async def test_include_parts_omitted_by_default(self, client, db_session):
        coll = WorkCollection(
            id=_uuid(),
            title_cn="A",
            external_source="tmdb_collection",
            external_id="131295",
        )
        db_session.add(coll)
        await db_session.commit()

        with patch(
            "app.api.v1.collections.fetch_tmdb_collection_parts",
            new=AsyncMock(return_value=[]),
        ) as fetch:
            res = await client.get(f"/api/v1/collections/{coll.id}")
        assert res.status_code == 200
        assert "untracked_parts" not in res.json()["data"]
        fetch.assert_not_called()

    async def test_include_parts_non_tmdb_collection_omits_key(self, client, db_session):
        coll = WorkCollection(
            id=_uuid(), title_cn="A", external_source="wikidata", external_id="Q200"
        )
        db_session.add(coll)
        await db_session.commit()

        res = await client.get(
            f"/api/v1/collections/{coll.id}", params={"include_parts": "true"}
        )
        assert res.status_code == 200
        assert "untracked_parts" not in res.json()["data"]

    async def test_include_parts_fetch_failure_returns_empty(self, client, db_session):
        coll = WorkCollection(
            id=_uuid(),
            title_cn="A",
            external_source="tmdb_collection",
            external_id="131295",
        )
        db_session.add(coll)
        await db_session.commit()

        with patch(
            "app.api.v1.collections.fetch_tmdb_collection_parts",
            new=AsyncMock(return_value=None),
        ):
            res = await client.get(
                f"/api/v1/collections/{coll.id}", params={"include_parts": "true"}
            )
        assert res.status_code == 200
        assert res.json()["data"]["untracked_parts"] == []
