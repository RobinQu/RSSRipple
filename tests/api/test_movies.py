"""API tests for Movie CRUD endpoints."""

from __future__ import annotations

import pytest


class TestMoviesCRUD:
    async def test_create_movie(self, client):
        res = await client.post("/api/v1/movies", json={
            "title_cn": "电影", "title_en": "Movie",
        })
        assert res.status_code == 201
        assert res.json()["data"]["title_en"] == "Movie"

    async def test_list_movies(self, client, sample_movie):
        res = await client.get("/api/v1/movies")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] >= 1

    async def test_get_movie(self, client):
        create = await client.post("/api/v1/movies", json={"title_en": "M"})
        mid = create.json()["data"]["id"]
        res = await client.get(f"/api/v1/movies/{mid}")
        assert res.status_code == 200

    async def test_update_movie(self, client):
        create = await client.post("/api/v1/movies", json={"title_en": "M"})
        mid = create.json()["data"]["id"]
        res = await client.put(f"/api/v1/movies/{mid}", json={"title_cn": "电影更新"})
        assert res.status_code == 200
        assert res.json()["data"]["title_cn"] == "电影更新"

    async def test_get_404(self, client):
        res = await client.get("/api/v1/movies/nope")
        assert res.status_code == 404

    async def test_delete(self, client):
        create = await client.post("/api/v1/movies", json={"title_en": "M"})
        mid = create.json()["data"]["id"]
        res = await client.delete(f"/api/v1/movies/{mid}")
        assert res.status_code == 200
        res2 = await client.get(f"/api/v1/movies/{mid}")
        assert res2.status_code == 404
