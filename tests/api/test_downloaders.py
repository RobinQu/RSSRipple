"""API tests for downloader endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


class TestDownloadersCRUD:
    async def test_create_downloader(self, client):
        res = await client.post("/api/v1/downloaders", json={
            "name": "DL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "/downloads/rssripple",
        })
        assert res.status_code == 201
        assert res.json()["data"]["name"] == "DL"
        assert res.json()["data"]["download_dir"] == "/downloads/rssripple"

    async def test_create_downloader_rejects_relative_download_dir(self, client):
        res = await client.post("/api/v1/downloaders", json={
            "name": "DL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "downloads/rssripple",
        })
        assert res.status_code == 422

    async def test_list_downloaders(self, client, sample_downloader):
        res = await client.get("/api/v1/downloaders")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] >= 1

    async def test_get_downloader(self, client, sample_downloader):
        res = await client.get(f"/api/v1/downloaders/{sample_downloader.id}")
        assert res.status_code == 200
        assert res.json()["data"]["id"] == sample_downloader.id

    async def test_update_downloader(self, client, sample_downloader):
        res = await client.put(
            f"/api/v1/downloaders/{sample_downloader.id}",
            json={"name": "Renamed"},
        )
        assert res.status_code == 200
        assert res.json()["data"]["name"] == "Renamed"

    async def test_delete_downloader(self, client, sample_downloader):
        res = await client.delete(f"/api/v1/downloaders/{sample_downloader.id}")
        assert res.status_code == 200
        res2 = await client.get(f"/api/v1/downloaders/{sample_downloader.id}")
        assert res2.status_code == 404

    async def test_get_404(self, client):
        res = await client.get("/api/v1/downloaders/nope")
        assert res.status_code == 404


class TestDownloaderActions:
    async def test_test_endpoint(self, client, sample_downloader, mock_transmission):
        res = await client.post(f"/api/v1/downloaders/{sample_downloader.id}/test")
        assert res.status_code == 200
        assert res.json()["data"]["success"] is True
        assert res.json()["data"]["free_space"] is not None

    async def test_torrents_live(self, client, sample_downloader, mock_transmission):
        res = await client.get(f"/api/v1/downloaders/{sample_downloader.id}/torrents")
        assert res.status_code == 200

    async def test_tasks_list(self, client, sample_downloader):
        res = await client.get(f"/api/v1/downloaders/{sample_downloader.id}/tasks")
        assert res.status_code == 200

    async def test_test_endpoint_failure(self, client, sample_downloader, mock_transmission):
        mock_transmission.test_connection.return_value = (False, "connection refused")
        res = await client.post(f"/api/v1/downloaders/{sample_downloader.id}/test")
        assert res.status_code == 200
        assert res.json()["data"]["success"] is False

    async def test_test_endpoint_free_space_failure(self, client, sample_downloader, mock_transmission):
        mock_transmission.free_space.side_effect = RuntimeError("no such directory")
        res = await client.post(f"/api/v1/downloaders/{sample_downloader.id}/test")
        assert res.status_code == 200
        assert res.json()["data"]["success"] is False
        assert "download_dir check failed" in res.json()["data"]["message"]

    async def test_torrents_live_error(self, client, sample_downloader, mock_transmission):
        mock_transmission.list_torrents.side_effect = Exception("conn err")
        res = await client.get(f"/api/v1/downloaders/{sample_downloader.id}/torrents")
        assert res.status_code == 502

    async def test_delete_rejects_linked_agents(self, client, sample_downloader):
        # Create an agent pointing at this downloader first
        with patch("app.api.v1.channels.validate_rss_url", AsyncMock(return_value=(True, "ok", 5, 5))):
            ch = await client.post("/api/v1/channels", json={
                "name": "DCh", "type": "rss_feed", "url": "https://x/rss",
                "field_mapping": TEST_FIELD_MAPPING,
            })
        ch_id = ch.json()["data"]["id"]
        await client.post("/api/v1/agents", json={
            "name": "DA", "channel_id": ch_id, "downloader_id": sample_downloader.id,
            "scope_channel_wide": True,
        })
        # Delete downloader
        res = await client.delete(f"/api/v1/downloaders/{sample_downloader.id}")
        assert res.status_code == 409

    async def test_test_404(self, client):
        res = await client.post("/api/v1/downloaders/nope/test")
        assert res.status_code == 404

    async def test_torrents_404(self, client):
        res = await client.get("/api/v1/downloaders/nope/torrents")
        assert res.status_code == 404

    async def test_tasks_404(self, client):
        res = await client.get("/api/v1/downloaders/nope/tasks")
        assert res.status_code == 404
