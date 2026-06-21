"""API tests for agent endpoints."""

import pytest


async def _create_channel(client):
    res = await client.post("/api/v1/channels", json={"name": "Ch", "url": "https://x.com/rss"})
    return res.json()["data"]["id"]


async def _create_downloader(client):
    res = await client.post("/api/v1/downloaders", json={"name": "TR", "type": "transmission", "url": "http://localhost:9091"})
    return res.json()["data"]["id"]


@pytest.mark.asyncio
async def test_create_agent(client):
    ch_id = await _create_channel(client)
    dl_id = await _create_downloader(client)

    res = await client.post("/api/v1/agents", json={
        "name": "Test Agent",
        "channel_id": ch_id,
        "downloader_id": dl_id,
        "llm_enabled": False,
        "filters": [
            {"field": "resolution", "operator": "eq", "value": "1080p", "priority": 10, "is_required": True},
        ],
    })
    assert res.status_code == 201
    data = res.json()["data"]
    assert data["name"] == "Test Agent"
    assert len(data["filters"]) == 1


@pytest.mark.asyncio
async def test_list_agents(client):
    ch_id = await _create_channel(client)
    dl_id = await _create_downloader(client)
    await client.post("/api/v1/agents", json={"name": "A1", "channel_id": ch_id, "downloader_id": dl_id})
    await client.post("/api/v1/agents", json={"name": "A2", "channel_id": ch_id, "downloader_id": dl_id})

    res = await client.get("/api/v1/agents")
    assert res.status_code == 200
    assert len(res.json()["data"]) == 2


@pytest.mark.asyncio
async def test_get_agent(client):
    ch_id = await _create_channel(client)
    dl_id = await _create_downloader(client)
    create_res = await client.post("/api/v1/agents", json={"name": "Agent X", "channel_id": ch_id, "downloader_id": dl_id})
    agent_id = create_res.json()["data"]["id"]

    res = await client.get(f"/api/v1/agents/{agent_id}")
    assert res.status_code == 200
    assert res.json()["data"]["name"] == "Agent X"


@pytest.mark.asyncio
async def test_delete_agent(client):
    ch_id = await _create_channel(client)
    dl_id = await _create_downloader(client)
    create_res = await client.post("/api/v1/agents", json={"name": "Delete Me", "channel_id": ch_id, "downloader_id": dl_id})
    agent_id = create_res.json()["data"]["id"]

    res = await client.delete(f"/api/v1/agents/{agent_id}")
    assert res.status_code == 200

    res = await client.get(f"/api/v1/agents/{agent_id}")
    assert res.status_code == 404
