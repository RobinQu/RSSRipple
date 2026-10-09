"""API tests for the /api-keys CRUD endpoints (stripped app, no middleware)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


class TestApiKeyExpiry:
    async def test_create_defaults_to_never_expires(self, client):
        res = await client.post("/api/v1/api-keys", json={"name": "ci bot"})
        assert res.status_code == 201
        assert res.json()["data"]["expires_at"] is None

    async def test_create_with_future_expiry(self, client):
        future = datetime.now(UTC) + timedelta(days=30)
        res = await client.post(
            "/api/v1/api-keys",
            json={"name": "short-lived", "expires_at": future.isoformat()},
        )
        assert res.status_code == 201
        stored = datetime.fromisoformat(res.json()["data"]["expires_at"])
        assert stored == future  # same instant, serialized as ISO 8601 UTC

        items = (await client.get("/api/v1/api-keys")).json()["data"]
        assert items[0]["expires_at"] == res.json()["data"]["expires_at"]

    async def test_create_with_past_expiry_422(self, client):
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        res = await client.post(
            "/api/v1/api-keys", json={"name": "x", "expires_at": past}
        )
        assert res.status_code == 422

    async def test_create_with_naive_expiry_422(self, client):
        naive = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None).isoformat()
        res = await client.post(
            "/api/v1/api-keys", json={"name": "x", "expires_at": naive}
        )
        assert res.status_code == 422


class TestApiKeyRotate:
    async def test_rotate_returns_new_plaintext_and_retires_old(self, client, db_session):
        created = await client.post("/api/v1/api-keys", json={"name": "ops"})
        old = created.json()["data"]

        res = await client.post(f"/api/v1/api-keys/{old['id']}/rotate")
        assert res.status_code == 200
        new = res.json()["data"]
        assert new["id"] != old["id"]
        assert new["name"] == "ops"
        assert new["key"].startswith("rr_")
        assert new["key"] != old["key"]
        assert new["expires_at"] is None  # inherited from the never-expiring old key

        items = (await client.get("/api/v1/api-keys")).json()["data"]
        assert [i["id"] for i in items] == [new["id"]]
        # The old plaintext no longer matches any stored key; the new one does.
        assert await check_api_key(db_session, old["key"]) is False
        assert await check_api_key(db_session, new["key"]) is True

    async def test_rotate_inherits_expiry(self, client):
        future = datetime.now(UTC) + timedelta(days=10)
        created = await client.post(
            "/api/v1/api-keys",
            json={"name": "ops", "expires_at": future.isoformat()},
        )
        old = created.json()["data"]

        res = await client.post(f"/api/v1/api-keys/{old['id']}/rotate", json={})
        assert res.status_code == 200
        assert res.json()["data"]["expires_at"] == old["expires_at"]

    async def test_rotate_accepts_new_expiry(self, client):
        created = await client.post("/api/v1/api-keys", json={"name": "ops"})
        old_id = created.json()["data"]["id"]
        future = datetime.now(UTC) + timedelta(days=7)

        res = await client.post(
            f"/api/v1/api-keys/{old_id}/rotate",
            json={"expires_at": future.isoformat()},
        )
        assert res.status_code == 200
        stored = datetime.fromisoformat(res.json()["data"]["expires_at"])
        assert stored == future

    async def test_rotate_with_past_expiry_422(self, client):
        created = await client.post("/api/v1/api-keys", json={"name": "ops"})
        old_id = created.json()["data"]["id"]
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

        res = await client.post(
            f"/api/v1/api-keys/{old_id}/rotate", json={"expires_at": past}
        )
        assert res.status_code == 422
        # The old key survives a rejected rotation.
        items = (await client.get("/api/v1/api-keys")).json()["data"]
        assert [i["id"] for i in items] == [old_id]

    async def test_rotate_missing_404(self, client):
        res = await client.post("/api/v1/api-keys/does-not-exist/rotate")
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "NOT_FOUND"
