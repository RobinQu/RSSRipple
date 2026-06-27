"""API tests for agent endpoints."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _uuid():
    return str(uuid.uuid4())


@pytest.fixture
async def channel_and_dl(client, api_mocks):
    """Create a channel and downloader via the API and return their IDs."""
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
    return ch.json()["data"]["id"], dl.json()["data"]["id"]


class TestAgentsCRUD:
    async def test_create_agent_minimal(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        res = await client.post("/api/v1/agents", json={
            "name": "My Agent",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "scope_channel_wide": True,
            "conflict_resolution": "ask",
        })
        assert res.status_code == 201
        data = res.json()["data"]
        assert data["name"] == "My Agent"
        assert data["scope_channel_wide"] is True
        assert data["status"] == "active"

    async def test_create_agent_requires_channel(self, client):
        res = await client.post("/api/v1/agents", json={
            "name": "X", "channel_id": "nonexistent",
        })
        assert res.status_code == 422

    async def test_create_agent_validates_filter_config(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        res = await client.post("/api/v1/agents", json={
            "name": "bad", "channel_id": ch_id, "downloader_id": dl_id,
            "filter_config": {"combinator": "and", "conditions": [
                {"field": "bogus", "operator": "eq", "value": "x"},
            ]},
        })
        assert res.status_code == 422
        assert "unknown field" in res.json()["error"]["message"]

    async def test_create_agent_rejects_too_many_works(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        works = [
            {"content_type": "tv", "series_id": _uuid()}
            for _ in range(11)
        ]
        res = await client.post("/api/v1/agents", json={
            "name": "too many", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": False, "works": works,
        })
        assert res.status_code == 422

    async def test_list_agents(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        res = await client.get("/api/v1/agents")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] >= 1

    async def test_get_agent(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        res = await client.get(f"/api/v1/agents/{aid}")
        assert res.status_code == 200
        assert res.json()["data"]["id"] == aid

    async def test_delete_agent(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        res = await client.delete(f"/api/v1/agents/{aid}")
        assert res.status_code == 200
        res2 = await client.get(f"/api/v1/agents/{aid}")
        assert res2.status_code == 404

    async def test_update_agent(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        res = await client.put(f"/api/v1/agents/{aid}", json={"name": "B"})
        assert res.status_code == 200
        assert res.json()["data"]["name"] == "B"


class TestAgentActions:
    async def test_run_enqueues(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        res = await client.post(f"/api/v1/agents/{aid}/run")
        assert res.status_code == 200

    async def test_run_status(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        res = await client.get(f"/api/v1/agents/{aid}/run-status")
        assert res.status_code == 200

    async def test_test_filters(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        res = await client.post(f"/api/v1/agents/{aid}/test-filters", json={})
        assert res.status_code == 200

    async def test_suggestions(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        res = await client.get(f"/api/v1/agents/{aid}/suggestions")
        assert res.status_code == 200


class TestAgentWorks:
    async def test_add_and_list_work(self, client, channel_and_dl, sample_series):
        ch_id, dl_id = channel_and_dl
        # Need to commit/persist sample_series into the test DB used by client.
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": False,
        })
        aid = create.json()["data"]["id"]
        # sample_series was created via the unit-test DB session fixture,
        # which uses a separate engine. Re-create the series through the API
        # or directly via a helper endpoint. Simpler: create a series first.
        s = await client.post("/api/v1/series", json={
            "title_cn": "剧", "title_en": "Show",
        })
        sid = s.json()["data"]["id"]
        res = await client.post(f"/api/v1/agents/{aid}/works", json={
            "content_type": "tv", "series_id": sid,
            "enable_episode_dedup": True,
        })
        assert res.status_code == 201
        lst = await client.get(f"/api/v1/agents/{aid}/works")
        assert lst.status_code == 200
        assert len(lst.json()["data"]) == 1

    async def test_work_requires_target(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": False,
        })
        aid = create.json()["data"]["id"]
        res = await client.post(f"/api/v1/agents/{aid}/works", json={
            "content_type": "tv",
        })
        assert res.status_code == 422

    async def test_delete_work(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": False,
        })
        aid = create.json()["data"]["id"]
        s = await client.post("/api/v1/series", json={
            "title_cn": "剧", "title_en": "Show",
        })
        sid = s.json()["data"]["id"]
        w = await client.post(f"/api/v1/agents/{aid}/works", json={
            "content_type": "tv", "series_id": sid,
        })
        wid = w.json()["data"]["id"]
        res = await client.delete(f"/api/v1/agents/{aid}/works/{wid}")
        assert res.status_code == 200
        lst = await client.get(f"/api/v1/agents/{aid}/works")
        assert len(lst.json()["data"]) == 0

    async def test_update_work(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": False,
        })
        aid = create.json()["data"]["id"]
        s = await client.post("/api/v1/series", json={"title_cn": "剧", "title_en": "Show"})
        sid = s.json()["data"]["id"]
        w = await client.post(f"/api/v1/agents/{aid}/works", json={
            "content_type": "tv", "series_id": sid, "enable_episode_dedup": True,
        })
        wid = w.json()["data"]["id"]
        upd = await client.put(f"/api/v1/agents/{aid}/works/{wid}",
                               json={"enable_episode_dedup": False})
        assert upd.status_code == 200
        assert upd.json()["data"]["enable_episode_dedup"] is False

    async def test_create_work_missing_target(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": False,
        })
        aid = create.json()["data"]["id"]
        res = await client.post(f"/api/v1/agents/{aid}/works", json={"content_type": "tv"})
        assert res.status_code == 422

    async def test_create_work_both_targets(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": False,
        })
        aid = create.json()["data"]["id"]
        s = await client.post("/api/v1/series", json={"title_cn": "剧"})
        m = await client.post("/api/v1/movies", json={"title_cn": "电影"})
        res = await client.post(f"/api/v1/agents/{aid}/works", json={
            "content_type": "tv",
            "series_id": s.json()["data"]["id"],
            "movie_id": m.json()["data"]["id"],
        })
        assert res.status_code == 422

    async def test_create_agent_missing_downloader(self, client, channel_and_dl):
        ch_id, _ = channel_and_dl
        res = await client.post("/api/v1/agents", json={
            "name": "bad", "channel_id": ch_id, "downloader_id": "nope",
        })
        assert res.status_code == 422

    async def test_run_already_running_returns_409(self, client, channel_and_dl, monkeypatch):
        from app.services import task_queue as tq_mod
        fake = MagicMock()
        fake.enqueue = AsyncMock(return_value=None)
        fake.status = AsyncMock(return_value={"status": "running"})
        monkeypatch.setattr(tq_mod, "task_queue", fake)
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id, "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        res = await client.post(f"/api/v1/agents/{aid}/run")
        assert res.status_code == 409

    async def test_update_agent_replace_works(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id, "scope_channel_wide": False,
        })
        aid = create.json()["data"]["id"]
        s1 = await client.post("/api/v1/series", json={"title_cn": "剧1"})
        s2 = await client.post("/api/v1/series", json={"title_cn": "剧2"})
        # Add first work via PUT update
        upd = await client.put(f"/api/v1/agents/{aid}", json={
            "works": [{"content_type": "tv", "series_id": s1.json()["data"]["id"]}],
        })
        assert upd.status_code == 200
        # Replace with second
        upd2 = await client.put(f"/api/v1/agents/{aid}", json={
            "works": [{"content_type": "tv", "series_id": s2.json()["data"]["id"]}],
        })
        assert upd2.status_code == 200
        lst = await client.get(f"/api/v1/agents/{aid}/works")
        assert len(lst.json()["data"]) == 1

    async def test_test_filters_with_resource_ids(self, client, channel_and_dl, db_session_factory):
        from app.models.file_resource import FileResource
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
            "filter_config": {"combinator": "and", "conditions": [
                {"field": "resolution", "operator": "eq", "value": "1080p"},
            ]},
        })
        aid = create.json()["data"]["id"]
        rid = str(uuid.uuid4())
        async with db_session_factory() as s:
            s.add(FileResource(id=rid, channel_id=ch_id, guid=rid+"g",
                               title_raw="R", resolution="1080p",
                               torrent_url="magnet:?xt=urn:btih:x"))
            await s.commit()
        res = await client.post(f"/api/v1/agents/{aid}/test-filters",
                                json={"resource_ids": [rid]})
        assert res.status_code == 200
        assert res.json()["data"]["total"] == 1
