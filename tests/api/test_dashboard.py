"""API tests for dashboard endpoint."""

from unittest.mock import AsyncMock, patch

import pytest

MOCK_VALIDATE = "app.api.v1.channels.validate_rss_url"
TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


@pytest.mark.asyncio
async def test_dashboard_empty(client):
    res = await client.get("/api/v1/dashboard")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["active_agents"] == 0
    assert data["active_channels"] == 0
    assert data["active_download_count"] == 0
    assert data["active_download_groups"] == []
    assert data["pending_decisions"] == []


@pytest.mark.asyncio
async def test_dashboard_split_endpoints(client):
    overview = await client.get("/api/v1/dashboard/overview")
    assert overview.status_code == 200
    overview_data = overview.json()["data"]
    assert overview_data["active_agents"] == 0
    assert overview_data["top_agents"] == []
    assert "active_download_groups" not in overview_data

    downloads = await client.get("/api/v1/dashboard/downloads")
    assert downloads.status_code == 200
    assert downloads.json()["data"] == {
        "active_download_count": 0,
        "active_download_groups": [],
    }


@pytest.mark.asyncio
async def test_dashboard_with_data(client):
    # Create channel + downloader + agent
    with patch(MOCK_VALIDATE, new_callable=AsyncMock, return_value=(True, "Valid", 10, 8)):
        ch = await client.post("/api/v1/channels", json={
            "name": "Ch",
            "url": "https://x.com/rss",
            "field_mapping": TEST_FIELD_MAPPING,
        })
    ch_id = ch.json()["data"]["id"]
    dl = await client.post("/api/v1/downloaders", json={"name": "TR", "type": "transmission", "url": "http://localhost:9091", "download_dir": "/downloads/rssripple"})
    dl_id = dl.json()["data"]["id"]
    await client.post("/api/v1/agents", json={"name": "A1", "channel_id": ch_id, "downloader_id": dl_id})

    res = await client.get("/api/v1/dashboard")
    data = res.json()["data"]
    assert data["active_agents"] == 1
    assert data["top_agents"][0]["name"] == "A1"
    assert data["top_agents"][0]["active_task_count"] == 0
