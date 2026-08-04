"""Additional API tests for download task controls (pause/resume/retry/delete)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _uuid():
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


async def _create_resource(db_session_factory, ch_id, title_raw):
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
    return {"id": rid}


@pytest.fixture
async def setup(client, mock_transmission, db_session_factory):
    with patch("app.api.v1.channels.validate_rss_url", AsyncMock(return_value=(True, "ok", 5, 5))):
        ch = await client.post("/api/v1/channels", json={
            "name": "CT", "type": "rss_feed",
            "url": "https://example.com/rss", "fetch_interval": 1800,
            "field_mapping": TEST_FIELD_MAPPING,
            "metadata_agent_enabled": False,
        })
    dl = await client.post("/api/v1/downloaders", json={
        "name": "DLT", "type": "transmission",
        "url": "http://127.0.0.1:9091/transmission/rpc",
        "download_dir": "/downloads/rssripple",
    })
    a = await client.post("/api/v1/agents", json={
        "name": "AT", "channel_id": ch.json()["data"]["id"],
        "downloader_id": dl.json()["data"]["id"], "scope_channel_wide": True,
    })
    ch_id = ch.json()["data"]["id"]
    dl_id = dl.json()["data"]["id"]
    aid = a.json()["data"]["id"]
    r = await _create_resource(db_session_factory, ch_id, "[G] TaskT - 01")
    return SimpleNamespace(ch_id=ch_id, dl_id=dl_id, aid=aid, rid=r["id"])


@pytest.fixture
async def with_task(setup, db_session_factory):
    from app.models.download_task import DownloadTask
    async with db_session_factory() as s:
        task = DownloadTask(
            id=_uuid(), agent_id=setup.aid, file_resource_id=setup.rid,
            downloader_id=setup.dl_id, transmission_torrent_id=42,
            download_dir="/downloads/rssripple/AgentA",
            status="downloading", progress=0.25,
            download_speed=0, upload_speed=0, retry_count=0, max_retries=3,
        )
        s.add(task)
        await s.commit()
        tid = task.id
    return SimpleNamespace(id=tid)


class TestTaskActions:
    async def test_pause_task(self, client, with_task, mock_transmission):
        res = await client.post(f"/api/v1/tasks/{with_task.id}/pause")
        assert res.status_code == 200
        assert res.json()["data"]["status"] == "paused"
        mock_transmission.pause_torrent.assert_awaited_once_with(42)

    async def test_resume_task(self, client, with_task, mock_transmission):
        res = await client.post(f"/api/v1/tasks/{with_task.id}/resume")
        assert res.status_code == 200
        assert res.json()["data"]["status"] == "queued"
        mock_transmission.resume_torrent.assert_awaited_once_with(42)

    async def test_retry_task(self, client, with_task, mock_transmission):
        res = await client.post(f"/api/v1/tasks/{with_task.id}/retry")
        assert res.status_code == 200
        assert res.json()["data"]["message"] == "retried"
        _, kwargs = mock_transmission.add_torrent.await_args
        assert kwargs["download_dir"] == "/downloads/rssripple/AgentA"

    async def test_delete_task(self, client, with_task, mock_transmission):
        res = await client.delete(f"/api/v1/tasks/{with_task.id}")
        assert res.status_code == 200
        assert res.json()["data"]["deleted"] is True
        mock_transmission.remove_torrent.assert_awaited()

    async def test_get_task(self, client, with_task):
        res = await client.get(f"/api/v1/tasks/{with_task.id}")
        assert res.status_code == 200
        assert res.json()["data"]["id"] == with_task.id

    async def test_list_agent_tasks(self, client, setup, with_task):
        res = await client.get(f"/api/v1/agents/{setup.aid}/tasks")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] >= 1


async def _create_task(db_session_factory, setup, status, agent_id="__default__"):
    from app.models.download_task import DownloadTask
    rid = await _create_resource(db_session_factory, setup.ch_id, f"[G] BatchT - {_uuid()}")
    tid = _uuid()
    async with db_session_factory() as s:
        s.add(DownloadTask(
            id=tid, agent_id=setup.aid if agent_id == "__default__" else agent_id,
            file_resource_id=rid["id"], downloader_id=setup.dl_id,
            transmission_torrent_id=42,
            download_dir="/downloads/rssripple/AgentA",
            status=status, progress=0.0,
            download_speed=0, upload_speed=0, retry_count=0, max_retries=3,
        ))
        await s.commit()
    return tid


class TestBatchRetry:
    async def test_retry_all_failed(self, client, setup, db_session_factory, mock_transmission):
        err1 = await _create_task(db_session_factory, setup, "error")
        err2 = await _create_task(db_session_factory, setup, "error")
        paused = await _create_task(db_session_factory, setup, "paused")
        completed = await _create_task(db_session_factory, setup, "completed")
        manual = await _create_task(db_session_factory, setup, "error", agent_id=None)

        res = await client.post(f"/api/v1/agents/{setup.aid}/tasks/batch-retry", json={})
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["processed"] == 3
        assert data["retried"] == 3
        assert data["failed"] == 0

        res = await client.get(f"/api/v1/agents/{setup.aid}/tasks?page_size=100")
        by_id = {t["id"]: t for t in res.json()["data"]}
        for tid in (err1, err2, paused):
            assert by_id[tid]["status"] == "downloading"
            assert by_id[tid]["retry_count"] == 1
            assert by_id[tid]["error_message"] is None
        assert by_id[completed]["status"] == "completed"
        # Manual task (agent_id=None) must not be touched.
        res = await client.get(f"/api/v1/tasks/{manual}")
        assert res.json()["data"]["status"] == "error"

    async def test_retry_selected_ids(self, client, setup, db_session_factory, mock_transmission):
        err1 = await _create_task(db_session_factory, setup, "error")
        err2 = await _create_task(db_session_factory, setup, "error")

        res = await client.post(
            f"/api/v1/agents/{setup.aid}/tasks/batch-retry",
            json={"task_ids": [err1]},
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["processed"] == 1
        assert data["retried"] == 1

        res = await client.get(f"/api/v1/agents/{setup.aid}/tasks?page_size=100")
        by_id = {t["id"]: t for t in res.json()["data"]}
        assert by_id[err1]["status"] == "downloading"
        assert by_id[err2]["status"] == "error"

    async def test_partial_failure(self, client, setup, db_session_factory, mock_transmission):
        await _create_task(db_session_factory, setup, "error")
        await _create_task(db_session_factory, setup, "error")
        mock_transmission.add_torrent.side_effect = [
            RuntimeError("boom"),
            {"torrent_id": 42, "name": "x", "hash": "h"},
        ]

        res = await client.post(f"/api/v1/agents/{setup.aid}/tasks/batch-retry", json={})
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["processed"] == 2
        assert data["retried"] == 1
        assert data["failed"] == 1
        assert len(data["errors"]) == 1
        assert "boom" in data["errors"][0]
