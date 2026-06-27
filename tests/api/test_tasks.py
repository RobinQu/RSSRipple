"""API tests for download task endpoints."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest


def _uuid():
    return str(uuid.uuid4())


@pytest.fixture
async def setup(client):
    """Create channel + downloader + agent and return their IDs."""
    with patch(
        "app.api.v1.channels.validate_rss_url",
        AsyncMock(return_value=(True, "ok", 5, 5)),
    ):
        ch = await client.post("/api/v1/channels", json={
            "name": "C", "type": "rss_feed",
            "url": "https://example.com/rss", "fetch_interval": 1800,
            "metadata_source": "none",
        })
    dl = await client.post("/api/v1/downloaders", json={
        "name": "DL", "type": "transmission",
        "url": "http://127.0.0.1:9091/transmission/rpc",
    })
    a = await client.post("/api/v1/agents", json={
        "name": "A",
        "channel_id": ch.json()["data"]["id"],
        "downloader_id": dl.json()["data"]["id"],
        "scope_channel_wide": True,
    })
    return ch.json()["data"]["id"], dl.json()["data"]["id"], a.json()["data"]["id"]


class TestTasksEndpoints:
    async def test_task_list_empty(self, client, setup):
        ch, dl, aid = setup
        res = await client.get(f"/api/v1/agents/{aid}/tasks")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] == 0

    async def test_task_404(self, client):
        res = await client.get("/api/v1/tasks/nope")
        assert res.status_code == 404

    async def test_list_with_status_filter(self, client, setup):
        ch, dl, aid = setup
        res = await client.get(f"/api/v1/agents/{aid}/tasks?status=downloading")
        assert res.status_code == 200

    async def test_pause_missing_task(self, client, setup):
        ch, dl, aid = setup
        res = await client.post(f"/api/v1/tasks/{_uuid()}/pause")
        assert res.status_code == 404

    async def test_retry_missing_task(self, client, setup):
        ch, dl, aid = setup
        res = await client.post(f"/api/v1/tasks/{_uuid()}/retry")
        assert res.status_code == 404

    async def test_resume_missing_task(self, client, setup):
        ch, dl, aid = setup
        res = await client.post(f"/api/v1/tasks/{_uuid()}/resume")
        assert res.status_code == 404

    async def test_delete_missing_task(self, client, setup):
        ch, dl, aid = setup
        res = await client.delete(f"/api/v1/tasks/{_uuid()}")
        assert res.status_code == 404
