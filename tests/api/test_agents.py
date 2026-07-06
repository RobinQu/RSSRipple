"""API tests for agent endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _uuid():
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


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
            "field_mapping": TEST_FIELD_MAPPING,
            "metadata_agent_enabled": False,
        })
    dl = await client.post("/api/v1/downloaders", json={
        "name": "DL", "type": "transmission",
        "url": "http://127.0.0.1:9091/transmission/rpc",
        "download_dir": "/downloads/rssripple",
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

    async def test_create_agent_defaults_conflict_resolution_to_auto(self, client, channel_and_dl):
        """Omitting conflict_resolution now defaults to 'auto'."""
        ch_id, dl_id = channel_and_dl
        res = await client.post("/api/v1/agents", json={
            "name": "Auto", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        assert res.status_code == 201
        assert res.json()["data"]["conflict_resolution"] == "auto"

    async def test_create_agent_persists_llm_prompt(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        res = await client.post("/api/v1/agents", json={
            "name": "P", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True, "llm_enabled": True,
            "llm_prompt": "Prefer LoliHouse HEVC releases.",
        })
        assert res.status_code == 201
        assert res.json()["data"]["llm_prompt"] == "Prefer LoliHouse HEVC releases."

    async def test_create_agent_with_download_subdir(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        res = await client.post("/api/v1/agents", json={
            "name": "My Agent",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "download_subdir": r"Anime\2026",
            "scope_channel_wide": True,
        })
        assert res.status_code == 201
        assert res.json()["data"]["download_subdir"] == "Anime/2026"

    async def test_create_agent_rejects_absolute_download_subdir(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        res = await client.post("/api/v1/agents", json={
            "name": "My Agent",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "download_subdir": "/absolute",
            "scope_channel_wide": True,
        })
        assert res.status_code == 422

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

    async def test_rules_preview_diff(self, client, channel_and_dl, db_session):
        """rules-preview returns newly/no_longer matching for a rule change."""
        from app.models.file_resource import FileResource
        from app.models.series import TVSeries

        ch_id, dl_id = channel_and_dl
        s = TVSeries(id=_uuid(), title_cn="S")
        db_session.add(s)
        await db_session.flush()
        r_keep = FileResource(
            id=_uuid(), channel_id=ch_id, guid=_uuid(),
            title_raw="[G] S - 01", torrent_url="magnet:?xt=urn:btih:x",
            series_id=s.id, episode=1, subtitle_group="NewSub",
        )
        r_drop = FileResource(
            id=_uuid(), channel_id=ch_id, guid=_uuid(),
            title_raw="[G] S - 02", torrent_url="magnet:?xt=urn:btih:y",
            series_id=s.id, episode=2, subtitle_group="OldSub",
        )
        db_session.add_all([r_keep, r_drop])
        await db_session.commit()

        # Existing agent subscribes to the series with filter subtitle_group=OldSub.
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": False,
            "works": [{"content_type": "tv", "series_id": s.id}],
            "filter_config": {"combinator": "and", "conditions": [
                {"field": "subtitle_group", "operator": "eq", "value": "OldSub"},
            ]},
        })
        aid = create.json()["data"]["id"]

        # Propose switching the filter to NewSub.
        res = await client.post("/api/v1/agents/rules-preview", json={
            "agent_id": aid,
            "scope_channel_wide": False,
            "filter_config": {"combinator": "and", "conditions": [
                {"field": "subtitle_group", "operator": "eq", "value": "NewSub"},
            ]},
            "works": [{"content_type": "tv", "series_id": s.id}],
        })
        assert res.status_code == 200
        data = res.json()["data"]
        newly_ids = {r["id"] for r in data["newly_matching"]}
        no_longer_ids = {r["id"] for r in data["no_longer_matching"]}
        assert newly_ids == {r_keep.id}
        assert no_longer_ids == {r_drop.id}

    async def test_rules_preview_requires_channel_for_create(self, client):
        """Without agent_id, channel_id is required (create-mode preview)."""
        res = await client.post("/api/v1/agents/rules-preview", json={
            "scope_channel_wide": True,
        })
        assert res.status_code == 422

    async def test_update_agent_advances_watermark_on_backfill(
        self, client, channel_and_dl, db_session
    ):
        """Saving with dispatch_resource_ids (even empty) advances the
        consumption watermark to the channel's max created_at."""
        from app.models.file_resource import FileResource

        ch_id, dl_id = channel_and_dl
        r = FileResource(
            id=_uuid(), channel_id=ch_id, guid=_uuid(),
            title_raw="[G] X - 01", torrent_url="magnet:?xt=urn:btih:z",
            series_id=None, movie_id=None,
        )
        db_session.add(r)
        await db_session.commit()
        max_created = r.created_at

        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        # Plain create (no dispatch_resource_ids) leaves watermark null.
        assert create.json()["data"]["last_consumed_at"] is None

        upd = await client.put(f"/api/v1/agents/{aid}", json={
            "name": "A2",
            "dispatch_resource_ids": [],
        })
        assert upd.status_code == 200
        wm = upd.json()["data"]["last_consumed_at"]
        assert wm is not None
        # Watermark is at least the resource's created_at.
        assert wm >= max_created.isoformat()

    async def test_update_with_works_and_dispatch_backfill_dispatches(
        self, client, channel_and_dl, db_session, mock_transmission
    ):
        """Regression: saving a works change + dispatch_resource_ids must
        actually dispatch the selected resources. Previously agent.works was
        stale (set to [] during replace) so process_resources saw no
        subscribed works and silently dispatched nothing."""
        from app.models.file_resource import FileResource
        from app.models.series import TVSeries

        ch_id, dl_id = channel_and_dl
        s = TVSeries(id=_uuid(), title_cn="S")
        db_session.add(s)
        await db_session.flush()
        r = FileResource(
            id=_uuid(), channel_id=ch_id, guid=_uuid(),
            title_raw="[G] S - 01", torrent_url="magnet:?xt=urn:btih:bk",
            series_id=s.id, episode=1, subtitle_group="G",
        )
        db_session.add(r)
        await db_session.commit()

        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": False, "works": [],
        })
        aid = create.json()["data"]["id"]

        upd = await client.put(f"/api/v1/agents/{aid}", json={
            "name": "A",
            "works": [{"content_type": "tv", "series_id": s.id}],
            "dispatch_resource_ids": [r.id],
        })
        assert upd.status_code == 200
        # The selected resource must have been dispatched (add_torrent called).
        mock_transmission.add_torrent.assert_awaited()
        # And a DownloadTask row exists for it.
        from sqlalchemy import select

        from app.models.download_task import DownloadTask
        task = (await db_session.execute(
            select(DownloadTask).where(DownloadTask.file_resource_id == r.id)
        )).scalars().first()
        assert task is not None

    async def test_list_agent_runs_returns_history(self, client, channel_and_dl, db_session):
        """GET /agents/{id}/runs returns persisted run records with matched
        resource summaries."""
        from app.models.agent_run import AgentRun
        from app.models.file_resource import FileResource

        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        r = FileResource(
            id=_uuid(), channel_id=ch_id, guid=_uuid(),
            title_raw="[G] R - 01", torrent_url="magnet:?xt=urn:btih:r",
        )
        db_session.add(r)
        await db_session.commit()
        async with db_session.begin():
            db_session.add(AgentRun(
                agent_id=aid, status="success", started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC), total_resources=1, matched=1,
                dispatched=1, matched_resource_ids=[r.id],
            ))
        res = await client.get(f"/api/v1/agents/{aid}/runs")
        assert res.status_code == 200
        data = res.json()["data"]
        assert len(data) == 1
        assert data[0]["status"] == "success"
        assert data[0]["matched_resource_ids"] == [r.id]
        assert data[0]["matched_resources"][0]["id"] == r.id

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
