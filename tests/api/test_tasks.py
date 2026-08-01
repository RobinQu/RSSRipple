"""API tests for download task endpoints."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest


def _uuid():
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


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
            "field_mapping": TEST_FIELD_MAPPING,
            "metadata_agent_enabled": False,
        })
    dl = await client.post("/api/v1/downloaders", json={
        "name": "DL", "type": "transmission",
        "url": "http://127.0.0.1:9091/transmission/rpc",
        "download_dir": "/downloads/rssripple",
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


async def _create_resource(db_session_factory, ch_id, title_raw):
    from datetime import UTC, datetime

    from app.models.file_resource import FileResource
    rid = _uuid()
    async with db_session_factory() as s:
        r = FileResource(
            id=rid, channel_id=ch_id, guid=_uuid(),
            title_raw=title_raw, search_title=title_raw,
            torrent_url=f"magnet:?xt=urn:btih:{rid}",
            parsed_at=datetime.now(UTC),
        )
        s.add(r)
        await s.commit()
    return rid


class TestManualTaskCreate:
    async def test_create_success(self, client, setup, db_session_factory, mock_transmission):
        ch, dl, aid = setup
        rid = await _create_resource(db_session_factory, ch, "[G] ShowA - 01")
        res = await client.post("/api/v1/tasks", json={
            "resource_id": rid, "downloader_id": dl,
        })
        assert res.status_code == 201
        data = res.json()["data"]
        assert data["agent_id"] is None
        assert data["file_resource_id"] == rid
        assert data["downloader_id"] == dl
        assert data["download_dir"] == "/downloads/rssripple"
        assert data["status"] == "downloading"
        assert data["transmission_torrent_id"] == 42
        assert data["confirmed_at"] is not None
        mock_transmission.add_torrent.assert_awaited()

    async def test_create_submit_failure_marks_error(
        self, client, setup, db_session_factory, mock_transmission
    ):
        ch, dl, aid = setup
        rid = await _create_resource(db_session_factory, ch, "[G] ShowA - 02")
        mock_transmission.add_torrent.side_effect = RuntimeError("boom")
        res = await client.post("/api/v1/tasks", json={
            "resource_id": rid, "downloader_id": dl,
        })
        assert res.status_code == 201
        data = res.json()["data"]
        assert data["status"] == "error"
        assert "boom" in data["error_message"]

    async def test_create_resource_not_found(self, client, setup):
        ch, dl, aid = setup
        res = await client.post("/api/v1/tasks", json={
            "resource_id": _uuid(), "downloader_id": dl,
        })
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "NOT_FOUND"

    async def test_create_downloader_not_found(
        self, client, setup, db_session_factory
    ):
        ch, dl, aid = setup
        rid = await _create_resource(db_session_factory, ch, "[G] ShowA - 03")
        res = await client.post("/api/v1/tasks", json={
            "resource_id": rid, "downloader_id": _uuid(),
        })
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "NOT_FOUND"
