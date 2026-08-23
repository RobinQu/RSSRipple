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

    async def test_create_agent_run_immediately_enqueues_full_run(self, client, channel_and_dl, monkeypatch):
        """run_immediately enqueues a background full-history run (scan_since=None)."""
        from app.services import task_queue as tq_mod

        ch_id, dl_id = channel_and_dl
        fake = MagicMock()
        fake.enqueue = AsyncMock(return_value={"task_id": "j1"})
        monkeypatch.setattr(tq_mod, "task_queue", fake)

        res = await client.post("/api/v1/agents", json={
            "name": "RunNow", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True, "run_immediately": True,
        })
        assert res.status_code == 201
        aid = res.json()["data"]["id"]
        fake.enqueue.assert_awaited_once()
        assert fake.enqueue.call_args.args[0] == "run_agent"
        assert fake.enqueue.call_args.args[1] == f"agent:{aid}"
        assert fake.enqueue.call_args.args[2] == {"agent_id": aid, "scan_since": None}

    async def test_create_agent_without_run_immediately_does_not_enqueue(self, client, channel_and_dl, monkeypatch):
        """Default (run_immediately=False) is a plain save — no background run."""
        from app.services import task_queue as tq_mod

        ch_id, dl_id = channel_and_dl
        fake = MagicMock()
        fake.enqueue = AsyncMock()
        monkeypatch.setattr(tq_mod, "task_queue", fake)

        res = await client.post("/api/v1/agents", json={
            "name": "Plain", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        assert res.status_code == 201
        fake.enqueue.assert_not_awaited()

    async def test_create_agent_pick_preferences_roundtrip(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        prefs = [
            {"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"},
            {"field": "resolution", "operator": "eq", "value": "1080p"},
        ]
        res = await client.post("/api/v1/agents", json={
            "name": "Prefs", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True, "pick_preferences": prefs,
        })
        assert res.status_code == 201
        aid = res.json()["data"]["id"]
        assert res.json()["data"]["pick_preferences"] == prefs

        got = await client.get(f"/api/v1/agents/{aid}")
        assert got.json()["data"]["pick_preferences"] == prefs

        # Update to a single rule, then clear with explicit null.
        put = await client.put(f"/api/v1/agents/{aid}", json={
            "pick_preferences": [prefs[0]],
        })
        assert put.status_code == 200
        assert put.json()["data"]["pick_preferences"] == [prefs[0]]
        put = await client.put(f"/api/v1/agents/{aid}", json={
            "pick_preferences": None,
        })
        assert put.status_code == 200
        assert put.json()["data"]["pick_preferences"] is None

    async def test_create_agent_invalid_pick_preferences_422(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        base = {
            "name": "BadPrefs", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        }
        # Unknown field
        res = await client.post("/api/v1/agents", json={
            **base, "pick_preferences": [
                {"field": "nope", "operator": "eq", "value": "x"},
            ],
        })
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
        # Operator not supported for the field type
        res = await client.post("/api/v1/agents", json={
            **base, "pick_preferences": [
                {"field": "subtitle_langs", "operator": "regex", "value": "x"},
            ],
        })
        assert res.status_code == 422
        # Value-taking operator with empty value
        res = await client.post("/api/v1/agents", json={
            **base, "pick_preferences": [
                {"field": "subtitle_group", "operator": "eq", "value": ""},
            ],
        })
        assert res.status_code == 422
        # Not a FieldCondition
        res = await client.post("/api/v1/agents", json={
            **base, "pick_preferences": [{"combinator": "and", "conditions": []}],
        })
        assert res.status_code == 422

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

    async def test_get_agent_includes_latest_completed_episode(
        self, client, channel_and_dl, db_session
    ):
        """GET /agents/{id} annotates TV works with the latest completed S/E."""
        from app.models.download_task import DownloadTask
        from app.models.file_resource import FileResource
        from app.models.series import TVSeries

        ch_id, dl_id = channel_and_dl
        s = TVSeries(id=_uuid(), title_cn="S")
        s_idle = TVSeries(id=_uuid(), title_cn="Idle")
        db_session.add_all([s, s_idle])
        await db_session.flush()

        def _res(guid, season, episode, is_batch=False):
            return FileResource(
                id=_uuid(), channel_id=ch_id, guid=guid,
                title_raw=guid, torrent_url=f"magnet:?xt=urn:btih:{guid}",
                series_id=s.id, season=season, episode=episode, is_batch=is_batch,
            )

        r13 = _res("e13", 1, 3)
        r15 = _res("e15", 1, 5)
        r_batch = _res("eb", 1, None, is_batch=True)  # excluded: batch
        r_active = _res("e18", 1, 8)                   # excluded: not completed
        db_session.add_all([r13, r15, r_batch, r_active])
        await db_session.flush()

        def _task(rid, status, completed=False):
            return DownloadTask(
                id=_uuid(), agent_id=None, file_resource_id=rid,
                downloader_id=dl_id, download_dir="/d", status=status,
                completed_at=datetime.now(UTC) if completed else None,
            )

        db_session.add_all([
            _task(r13.id, "completed", completed=True),
            # Organize(move) changes a completed task to cancelled but keeps
            # completed_at; it must continue contributing library progress.
            _task(r15.id, "cancelled", completed=True),
            _task(r_active.id, "downloading"),
        ])
        await db_session.commit()

        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": False,
            "works": [
                {"content_type": "tv", "series_id": s.id},
                {"content_type": "tv", "series_id": s_idle.id},
            ],
        })
        aid = create.json()["data"]["id"]
        res = await client.get(f"/api/v1/agents/{aid}")
        assert res.status_code == 200
        works = {w["series_id"]: w for w in res.json()["data"]["works"]}
        assert works[s.id]["latest_completed_season"] == 1
        assert works[s.id]["latest_completed_episode"] == 5
        assert works[s_idle.id]["latest_completed_season"] is None
        assert works[s_idle.id]["latest_completed_episode"] is None

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

    async def test_list_agent_runs_non_empty_filter(self, client, channel_and_dl, db_session):
        """GET /agents/{id}/runs?non_empty=true hides routine no-op runs."""
        from app.models.agent_run import AgentRun
        from app.models.pending_decision import PendingDecision

        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        now = datetime.now(UTC)
        async with db_session.begin():
            db_session.add_all([
                # no-op routine run -> hidden
                AgentRun(agent_id=aid, status="success", started_at=now,
                         finished_at=now, total_resources=5, matched=0),
                # dispatched -> kept
                AgentRun(agent_id=aid, status="success", started_at=now,
                         finished_at=now, total_resources=5, matched=1, dispatched=1),
                # produced pending decisions -> kept
                AgentRun(agent_id=aid, status="pending_decisions", started_at=now,
                         finished_at=now, total_resources=5, matched=1,
                         pending_decisions=1),
                # running -> kept
                AgentRun(agent_id=aid, status="running", started_at=now),
                # failed (even with zero counts) -> kept, errors stay visible
                AgentRun(agent_id=aid, status="failed", started_at=now,
                         finished_at=now, errors=["boom"]),
            ])
        # An open pending decision keeps the frozen run status visible as-is;
        # with none, the read-time correction would present it as "success".
        async with db_session.begin():
            db_session.add(PendingDecision(
                agent_id=aid, status="pending", candidates=["x"], reason="冲突",
            ))
        res = await client.get(f"/api/v1/agents/{aid}/runs?non_empty=true")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] == 4
        statuses = {r["status"] for r in res.json()["data"]}
        assert statuses == {"success", "pending_decisions", "running", "failed"}
        # success appears twice (dispatched run + hidden no-op would be 3)
        assert sum(1 for r in res.json()["data"] if r["status"] == "success") == 1
        # Default (no param) returns everything.
        res = await client.get(f"/api/v1/agents/{aid}/runs")
        assert res.json()["meta"]["total"] == 5

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

    async def test_run_with_scan_since_enqueues(self, client, channel_and_dl):
        """Windowed run: a past scan_since is accepted and enqueued."""
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        res = await client.post(
            f"/api/v1/agents/{aid}/run",
            json={"scan_since": "2026-07-18T09:00:00Z"},
        )
        assert res.status_code == 200

    async def test_run_with_scan_since_null_enqueues(self, client, channel_and_dl):
        """Windowed run: explicit null means "no limit" (full history)."""
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        res = await client.post(
            f"/api/v1/agents/{aid}/run", json={"scan_since": None},
        )
        assert res.status_code == 200

    async def test_run_with_future_scan_since_422(self, client, channel_and_dl):
        """A scan_since in the future is rejected with VALIDATION_ERROR."""
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        future = datetime.now(UTC).replace(year=datetime.now(UTC).year + 1)
        res = await client.post(
            f"/api/v1/agents/{aid}/run",
            json={"scan_since": future.isoformat()},
        )
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_run_status(self, client, channel_and_dl):
        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        res = await client.get(f"/api/v1/agents/{aid}/run-status")
        assert res.status_code == 200

    async def test_filter_gating_with_channel_required_fields(self, client, channel_and_dl):
        """When the channel declares required metadata fields, agent filter
        DSL is gated to resource-level fields + the declared work fields."""
        ch_id, dl_id = channel_and_dl
        # Declare rating only.
        res = await client.put(
            f"/api/v1/channels/{ch_id}", json={"required_metadata_fields": ["rating"]}
        )
        assert res.status_code == 200

        def _agent_payload(filter_config):
            return {
                "name": "Gated", "channel_id": ch_id, "downloader_id": dl_id,
                "scope_channel_wide": True, "filter_config": filter_config,
            }

        # Declared work field → allowed.
        ok = await client.post("/api/v1/agents", json=_agent_payload(
            {"combinator": "and", "conditions": [
                {"field": "series.rating", "operator": "gte", "value": 8},
            ]},
        ))
        assert ok.status_code == 201, ok.text

        # Resource-level fields are always allowed.
        ok2 = await client.post("/api/v1/agents", json=_agent_payload(
            {"combinator": "and", "conditions": [
                {"field": "resolution", "operator": "eq", "value": "1080p"},
            ]},
        ))
        assert ok2.status_code == 201, ok2.text

        # Undeclared work field → 422 (year/is_anime ride in with the locked
        # baseline, so a genuinely opt-in pair like collection is used here).
        bad = await client.post("/api/v1/agents", json=_agent_payload(
            {"combinator": "and", "conditions": [
                {"field": "series.collection", "operator": "contains", "value": "X"},
            ]},
        ))
        assert bad.status_code == 422
        assert "series.collection" in bad.json()["error"]["message"]

        # Update path gates the same way.
        aid = ok.json()["data"]["id"]
        bad_put = await client.put(f"/api/v1/agents/{aid}", json={
            "filter_config": {"combinator": "and", "conditions": [
                {"field": "movie.genre", "operator": "contains", "value": "Drama"},
            ]},
        })
        assert bad_put.status_code == 422
        assert "movie.genre" in bad_put.json()["error"]["message"]

        # pick_preferences are exempt from the gate.
        ok_pref = await client.put(f"/api/v1/agents/{aid}", json={
            "pick_preferences": [
                {"field": "series.year", "operator": "gte", "value": 2020},
            ],
        })
        assert ok_pref.status_code == 200, ok_pref.text

    async def test_filter_gating_baseline_when_channel_undeclared(
        self, client, channel_and_dl
    ):
        """A fresh channel carries the locked baseline (no "unrestricted"
        state): resource-level fields stay allowed, undeclared work fields
        are rejected."""
        ch_id, dl_id = channel_and_dl
        res = await client.post("/api/v1/agents", json={
            "name": "Free", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
            "filter_config": {"combinator": "and", "conditions": [
                {"field": "series.rating", "operator": "gte", "value": 8},
            ]},
        })
        assert res.status_code == 422
        assert "series.rating" in res.json()["error"]["message"]

        # Resource-level fields remain allowed under the baseline gate.
        ok = await client.post("/api/v1/agents", json={
            "name": "Free2", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
            "filter_config": {"combinator": "and", "conditions": [
                {"field": "resolution", "operator": "eq", "value": "1080p"},
            ]},
        })
        assert ok.status_code == 201, ok.text

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


class TestAgentRunsPendingDecisionCorrection:
    """Read-time correction of frozen run snapshots on GET /agents/{id}/runs.

    A run frozen as "pending_decisions" is presented as "success" once the
    agent has no pending decisions left (response only — the DB row keeps its
    snapshot), and matched resources carry a "pending_decision" marker while
    their decision is still open.
    """

    async def test_pending_run_kept_and_marked_then_corrected(
        self, client, channel_and_dl, db_session, db_session_factory
    ):
        from sqlalchemy import select

        from app.models.agent_run import AgentRun
        from app.models.file_resource import FileResource
        from app.models.pending_decision import PendingDecision

        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        r1 = FileResource(
            id=_uuid(), channel_id=ch_id, guid=_uuid(),
            title_raw="[G] PD - 01", torrent_url="magnet:?xt=urn:btih:pd1",
        )
        r2 = FileResource(
            id=_uuid(), channel_id=ch_id, guid=_uuid(),
            title_raw="[G2] PD - 01", torrent_url="magnet:?xt=urn:btih:pd2",
        )
        db_session.add_all([r1, r2])
        await db_session.commit()
        run_id = _uuid()
        async with db_session.begin():
            db_session.add(AgentRun(
                id=run_id, agent_id=aid, status="pending_decisions",
                started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
                total_resources=2, matched=2, pending_decisions=1,
                matched_resource_ids=[r1.id, r2.id],
            ))
        did = _uuid()
        async with db_session_factory() as s:
            s.add(PendingDecision(
                id=did, agent_id=aid, status="pending",
                candidates=[r1.id, r2.id], reason="冲突",
            ))
            await s.commit()

        # While a pending decision exists: run status stays as snapshotted and
        # the candidate resources are marked pending_decision=true.
        res = await client.get(f"/api/v1/agents/{aid}/runs")
        assert res.status_code == 200
        data = res.json()["data"]
        assert len(data) == 1
        assert data[0]["status"] == "pending_decisions"
        assert {m["pending_decision"] for m in data[0]["matched_resources"]} == {True}

        # Decision handled -> read-time correction kicks in.
        async with db_session_factory() as s:
            pd = await s.get(PendingDecision, did)
            pd.status = "decided"
            await s.commit()
        res = await client.get(f"/api/v1/agents/{aid}/runs")
        data = res.json()["data"]
        assert data[0]["status"] == "success"
        # Historical snapshot fields stay untouched in the response.
        assert data[0]["pending_decisions"] == 1
        assert {m["pending_decision"] for m in data[0]["matched_resources"]} == {False}
        # The DB row itself is never rewritten.
        async with db_session_factory() as s:
            row = (await s.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )).scalar_one()
            assert row.status == "pending_decisions"

    async def test_unrelated_resources_not_marked(
        self, client, channel_and_dl, db_session, db_session_factory
    ):
        """Matched resources that are not candidates of any pending decision
        get pending_decision=false even while a decision is open."""
        from app.models.agent_run import AgentRun
        from app.models.file_resource import FileResource
        from app.models.pending_decision import PendingDecision

        ch_id, dl_id = channel_and_dl
        create = await client.post("/api/v1/agents", json={
            "name": "A", "channel_id": ch_id, "downloader_id": dl_id,
            "scope_channel_wide": True,
        })
        aid = create.json()["data"]["id"]
        r_cand = FileResource(
            id=_uuid(), channel_id=ch_id, guid=_uuid(),
            title_raw="[G] UN - 01", torrent_url="magnet:?xt=urn:btih:un1",
        )
        r_other = FileResource(
            id=_uuid(), channel_id=ch_id, guid=_uuid(),
            title_raw="[G] UN - 02", torrent_url="magnet:?xt=urn:btih:un2",
        )
        db_session.add_all([r_cand, r_other])
        await db_session.commit()
        async with db_session.begin():
            db_session.add(AgentRun(
                agent_id=aid, status="pending_decisions",
                started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
                total_resources=2, matched=2, pending_decisions=1,
                matched_resource_ids=[r_cand.id, r_other.id],
            ))
        async with db_session_factory() as s:
            s.add(PendingDecision(
                id=_uuid(), agent_id=aid, status="pending",
                candidates=[r_cand.id], reason="冲突",
            ))
            await s.commit()
        res = await client.get(f"/api/v1/agents/{aid}/runs")
        marks = {
            m["id"]: m["pending_decision"]
            for m in res.json()["data"][0]["matched_resources"]
        }
        assert marks == {r_cand.id: True, r_other.id: False}
