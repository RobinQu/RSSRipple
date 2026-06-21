"""API tests for dashboard endpoint."""

import pytest


@pytest.mark.asyncio
async def test_dashboard_empty(client):
    res = await client.get("/api/v1/dashboard")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["active_agents"] == 0
    assert data["active_downloads"] == []
    assert data["pending_decisions"] == []


@pytest.mark.asyncio
async def test_dashboard_with_data(client):
    # Create channel + downloader + agent
    ch = await client.post("/api/v1/channels", json={"name": "Ch", "url": "https://x.com/rss"})
    ch_id = ch.json()["data"]["id"]
    dl = await client.post("/api/v1/downloaders", json={"name": "TR", "type": "transmission", "url": "http://localhost:9091"})
    dl_id = dl.json()["data"]["id"]
    await client.post("/api/v1/agents", json={"name": "A1", "channel_id": ch_id, "downloader_id": dl_id})

    res = await client.get("/api/v1/dashboard")
    data = res.json()["data"]
    assert data["active_agents"] == 1
