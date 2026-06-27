"""API tests for TVSeries CRUD endpoints."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest


def _uuid():
    return str(uuid.uuid4())


class TestSeriesCRUD:
    async def test_create_series(self, client):
        res = await client.post("/api/v1/series", json={
            "title_cn": "剧", "title_en": "Show",
        })
        assert res.status_code == 201
        assert res.json()["data"]["title_cn"] == "剧"

    async def test_list_series(self, client, sample_series):
        res = await client.get("/api/v1/series")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] >= 1

    async def test_get_series(self, client):
        create = await client.post("/api/v1/series", json={"title_en": "S"})
        sid = create.json()["data"]["id"]
        res = await client.get(f"/api/v1/series/{sid}")
        assert res.status_code == 200

    async def test_update_series(self, client):
        create = await client.post("/api/v1/series", json={"title_en": "S"})
        sid = create.json()["data"]["id"]
        res = await client.put(f"/api/v1/series/{sid}", json={"title_cn": "剧更新"})
        assert res.status_code == 200
        assert res.json()["data"]["title_cn"] == "剧更新"

    async def test_get_404(self, client):
        res = await client.get("/api/v1/series/nope")
        assert res.status_code == 404

    async def test_update_404(self, client):
        res = await client.put("/api/v1/series/nope", json={"title_cn": "x"})
        assert res.status_code == 404

    async def test_delete_nullifies_fks(self, client):
        # Create a series and a resource pointing to it.
        s = await client.post("/api/v1/series", json={"title_en": "Series"})
        sid = s.json()["data"]["id"]
        res = await client.delete(f"/api/v1/series/{sid}")
        assert res.status_code == 200
        res2 = await client.get(f"/api/v1/series/{sid}")
        assert res2.status_code == 404
