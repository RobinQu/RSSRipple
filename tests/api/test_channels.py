"""API tests for channel endpoints."""

import pytest


@pytest.mark.asyncio
async def test_create_channel(client):
    res = await client.post("/api/v1/channels", json={
        "name": "Test Channel",
        "type": "rss_feed",
        "url": "https://example.com/rss",
        "fetch_interval": 1800,
    })
    assert res.status_code == 201
    data = res.json()
    assert data["success"] is True
    assert data["data"]["name"] == "Test Channel"
    assert data["data"]["status"] == "active"


@pytest.mark.asyncio
async def test_list_channels(client):
    # Create two channels
    await client.post("/api/v1/channels", json={"name": "Ch1", "url": "https://a.com/rss"})
    await client.post("/api/v1/channels", json={"name": "Ch2", "url": "https://b.com/rss"})

    res = await client.get("/api/v1/channels")
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert len(data["data"]) == 2
    assert data["meta"]["total"] == 2


@pytest.mark.asyncio
async def test_get_channel(client):
    create_res = await client.post("/api/v1/channels", json={"name": "My Channel", "url": "https://x.com/rss"})
    channel_id = create_res.json()["data"]["id"]

    res = await client.get(f"/api/v1/channels/{channel_id}")
    assert res.status_code == 200
    assert res.json()["data"]["name"] == "My Channel"


@pytest.mark.asyncio
async def test_get_channel_not_found(client):
    res = await client.get("/api/v1/channels/nonexistent-id")
    assert res.status_code == 404
    assert res.json()["success"] is False


@pytest.mark.asyncio
async def test_update_channel(client):
    create_res = await client.post("/api/v1/channels", json={"name": "Old Name", "url": "https://x.com/rss"})
    channel_id = create_res.json()["data"]["id"]

    res = await client.put(f"/api/v1/channels/{channel_id}", json={"name": "New Name"})
    assert res.status_code == 200
    assert res.json()["data"]["name"] == "New Name"


@pytest.mark.asyncio
async def test_delete_channel(client):
    create_res = await client.post("/api/v1/channels", json={"name": "Delete Me", "url": "https://x.com/rss"})
    channel_id = create_res.json()["data"]["id"]

    res = await client.delete(f"/api/v1/channels/{channel_id}")
    assert res.status_code == 200
    assert res.json()["data"]["deleted"] is True

    # Verify deleted
    res = await client.get(f"/api/v1/channels/{channel_id}")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_validate_url(client):
    res = await client.post("/api/v1/channels/validate-url", json={"url": "https://example.com/rss"})
    assert res.status_code == 200
    assert res.json()["success"] is True
