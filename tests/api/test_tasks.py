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


class TestGlobalTaskList:
    async def _create_task(self, db_session_factory, ch_id, dl_id, status="downloading",
                           agent_id=None, created_at=None):
        from app.models.download_task import DownloadTask
        rid = await _create_resource(db_session_factory, ch_id, f"[G] GlobalT - {_uuid()}")
        tid = _uuid()
        async with db_session_factory() as s:
            s.add(DownloadTask(
                id=tid, agent_id=agent_id, file_resource_id=rid,
                downloader_id=dl_id, transmission_torrent_id=42,
                download_dir="/downloads/rssripple/AgentA",
                status=status, progress=0.0,
                download_speed=0, upload_speed=0, retry_count=0, max_retries=3,
                **({"created_at": created_at} if created_at else {}),
            ))
            await s.commit()
        return tid

    async def test_default_list_newest_first(self, client, setup, db_session_factory):
        from datetime import UTC, datetime
        ch, dl, aid = setup
        t1 = await self._create_task(db_session_factory, ch, dl,
                                     created_at=datetime(2024, 1, 1, tzinfo=UTC))
        t2 = await self._create_task(db_session_factory, ch, dl,
                                     created_at=datetime(2024, 1, 3, tzinfo=UTC))
        t3 = await self._create_task(db_session_factory, ch, dl,
                                     created_at=datetime(2024, 1, 2, tzinfo=UTC))
        res = await client.get("/api/v1/tasks")
        assert res.status_code == 200
        body = res.json()
        assert body["meta"]["total"] == 3
        assert [t["id"] for t in body["data"]] == [t2, t3, t1]
        item = body["data"][0]
        assert item["downloader_id"] == dl
        assert "file_resource" in item and "agent" in item

    async def test_filter_downloader_id(self, client, setup, db_session_factory):
        ch, dl, aid = setup
        await self._create_task(db_session_factory, ch, dl)
        res = await client.get(f"/api/v1/tasks?downloader_id={dl}")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] == 1
        res = await client.get(f"/api/v1/tasks?downloader_id={_uuid()}")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] == 0

    async def test_filter_agent_id(self, client, setup, db_session_factory):
        ch, dl, aid = setup
        await self._create_task(db_session_factory, ch, dl, agent_id=aid)
        await self._create_task(db_session_factory, ch, dl, agent_id=None)
        res = await client.get(f"/api/v1/tasks?agent_id={aid}")
        assert res.status_code == 200
        body = res.json()
        assert body["meta"]["total"] == 1
        assert body["data"][0]["agent_id"] == aid

    async def test_filter_status(self, client, setup, db_session_factory):
        ch, dl, aid = setup
        await self._create_task(db_session_factory, ch, dl, status="error")
        await self._create_task(db_session_factory, ch, dl, status="completed")
        res = await client.get("/api/v1/tasks?status=error")
        assert res.status_code == 200
        body = res.json()
        assert body["meta"]["total"] == 1
        assert body["data"][0]["status"] == "error"

    async def test_invalid_status_422(self, client, setup):
        res = await client.get("/api/v1/tasks?status=bogus")
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_pagination(self, client, setup, db_session_factory):
        ch, dl, aid = setup
        for _ in range(3):
            await self._create_task(db_session_factory, ch, dl)
        res = await client.get("/api/v1/tasks?page=1&page_size=2")
        assert res.status_code == 200
        body = res.json()
        assert len(body["data"]) == 2
        assert body["meta"]["total"] == 3
        res = await client.get("/api/v1/tasks?page=2&page_size=2")
        assert len(res.json()["data"]) == 1

    async def test_page_size_cap(self, client, setup):
        res = await client.get("/api/v1/tasks?page_size=101")
        assert res.status_code == 422
