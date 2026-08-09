"""API tests for the /api-keys CRUD endpoints (stripped app, no middleware)."""

from __future__ import annotations

from app.services.auth_service import check_api_key


class TestApiKeys:
    async def test_create_returns_plaintext_once(self, client):
        res = await client.post("/api/v1/api-keys", json={"name": "ci bot"})
        assert res.status_code == 201
        data = res.json()["data"]
        assert data["name"] == "ci bot"
        assert data["key"].startswith("rr_")
        assert data["prefix"] == data["key"][:10]
        assert data["id"]
        assert data["created_at"]

    async def test_list_never_exposes_secret(self, client):
        created = await client.post("/api/v1/api-keys", json={"name": "ci bot"})
        plaintext = created.json()["data"]["key"]

        res = await client.get("/api/v1/api-keys")
        assert res.status_code == 200
        items = res.json()["data"]
        assert len(items) == 1
        item = items[0]
        assert item["name"] == "ci bot"
        assert item["prefix"] == plaintext[:10]
        assert "key" not in item
        assert "key_hash" not in item
        assert plaintext not in res.text

    async def test_multiple_keys_are_distinct(self, client):
        a = await client.post("/api/v1/api-keys", json={"name": "a"})
        b = await client.post("/api/v1/api-keys", json={"name": "b"})
        assert a.json()["data"]["key"] != b.json()["data"]["key"]
        items = (await client.get("/api/v1/api-keys")).json()["data"]
        assert len(items) == 2

    async def test_created_key_authenticates(self, client, db_session):
        res = await client.post("/api/v1/api-keys", json={"name": "ops"})
        plaintext = res.json()["data"]["key"]
        assert await check_api_key(db_session, plaintext) is True
        assert await check_api_key(db_session, "rr_wrong") is False

    async def test_delete(self, client, db_session):
        created = await client.post("/api/v1/api-keys", json={"name": "temp"})
        data = created.json()["data"]

        res = await client.delete(f"/api/v1/api-keys/{data['id']}")
        assert res.status_code == 200
        assert res.json()["data"]["deleted"] is True

        assert (await client.get("/api/v1/api-keys")).json()["data"] == []
        assert await check_api_key(db_session, data["key"]) is False

    async def test_delete_missing_404(self, client):
        res = await client.delete("/api/v1/api-keys/does-not-exist")
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "NOT_FOUND"

    async def test_empty_name_rejected(self, client):
        res = await client.post("/api/v1/api-keys", json={"name": ""})
        assert res.status_code == 422
