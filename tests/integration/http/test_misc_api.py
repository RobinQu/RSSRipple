"""Miscellaneous API surface integration tests.

Covers smaller endpoint paths not exercised by the focused suites:

  - POST /channels/validate-url (valid + unreachable)
  - POST /channels/preview-feed (raw + with field_mapping; bad URL → 400)
  - Fetch failure path: channel URL changed to a broken feed → failed status
  - Downloader detail / update / delete-409-while-bound / live torrents error
  - Agent works subresource PUT; agent create validation (422s)

Requirements: Docker test environment with app + test-server services.
"""

from __future__ import annotations

import time

from tests.integration.http._http import (
    DEFAULT_FIELD_MAPPING,
    RICH_FIELD_MAPPING,
    TEST_SERVER,
    _api,
)

GOOD_FEED = f"{TEST_SERVER}/rss/mikanani?series=2"
BROKEN_FEED = f"{TEST_SERVER}/rss/mikanani?series=99"  # server-side 500


class TestValidateAndPreview:
    def test_validate_url_valid(self):
        r = _api("/api/v1/channels/validate-url", method="post", json={"url": GOOD_FEED})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["valid"] is True
        assert data["item_count"] > 0

    def test_validate_url_unreachable(self):
        r = _api(
            "/api/v1/channels/validate-url",
            method="post",
            json={"url": "http://test-server:1/rss"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["valid"] is False

    def test_preview_feed_raw(self):
        r = _api("/api/v1/channels/preview-feed", method="post", json={"url": GOOD_FEED})
        assert r.status_code == 200, f"preview failed: {r.text}"
        data = r.json()["data"]
        assert len(data["entries"]) > 0
        assert data["parsed"] == []

    def test_preview_feed_with_mapping(self):
        r = _api(
            "/api/v1/channels/preview-feed",
            method="post",
            json={"url": GOOD_FEED, "field_mapping": RICH_FIELD_MAPPING},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data["parsed"]) > 0
        sample = data["parsed"][0]
        assert sample.get("episode") is not None
        assert sample.get("subtitle_group")

    def test_preview_feed_bad_url(self):
        # Unreachable URL: get_raw_entries degrades to an empty entry list
        # rather than raising (feedparser swallows connection errors).
        r = _api(
            "/api/v1/channels/preview-feed",
            method="post",
            json={"url": "http://test-server:1/rss"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["entries"] == []


class TestFetchFailurePath:
    def test_broken_feed_marks_channel_failed(self):
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "Fetch Failure Channel",
                "url": GOOD_FEED,
                "field_mapping": DEFAULT_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": False,
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.text}"
        ch_id = r.json()["data"]["id"]

        try:
            # Point the channel at a feed that 500s, then fetch.
            r = _api(
                f"/api/v1/channels/{ch_id}",
                method="put",
                json={"url": BROKEN_FEED},
            )
            assert r.status_code == 200, f"update url failed: {r.text}"

            _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
            deadline = time.time() + 120
            status = None
            while time.time() < deadline:
                r = _api(f"/api/v1/channels/{ch_id}/fetch-status")
                status = (r.json().get("data") or {}).get("status")
                if status in ("done", "failed"):
                    break
                time.sleep(2)
            assert status in ("done", "failed"), f"fetch never terminated: {status}"

            r = _api(f"/api/v1/channels/{ch_id}")
            data = r.json()["data"]
            assert data["last_fetch_status"] == "failed", (
                f"expected failed fetch status: {data['last_fetch_status']}"
            )
            assert data["status"] == "error"
            assert data["last_fetch_error"]
        finally:
            _api(f"/api/v1/channels/{ch_id}", method="delete")


class TestDownloaderDetails:
    def test_detail_update_and_delete_409(self):
        r = _api(
            "/api/v1/downloaders",
            method="post",
            json={"name": "Detail Mock", "type": "mock"},
        )
        assert r.status_code == 201
        dl_id = r.json()["data"]["id"]

        r = _api(f"/api/v1/downloaders/{dl_id}")
        assert r.status_code == 200
        assert r.json()["data"]["name"] == "Detail Mock"

        r = _api(
            f"/api/v1/downloaders/{dl_id}",
            method="put",
            json={"name": "Detail Mock Renamed"},
        )
        assert r.status_code == 200, f"update failed: {r.text}"
        assert r.json()["data"]["name"] == "Detail Mock Renamed"

        # Bind an agent → delete must 409 with the agent list
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "Downloader 409 Channel",
                "url": GOOD_FEED,
                "field_mapping": DEFAULT_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": False,
            },
        )
        assert r.status_code == 201
        ch_id = r.json()["data"]["id"]
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Bound Agent",
                "channel_id": ch_id,
                "downloader_id": dl_id,
                "scope_channel_wide": True,
                "llm_enabled": False,
            },
        )
        assert r.status_code == 201
        agent_id = r.json()["data"]["id"]

        r = _api(f"/api/v1/downloaders/{dl_id}", method="delete")
        assert r.status_code == 409, f"expected 409, got {r.status_code}"
        agents = (r.json().get("error") or {}).get("details", {}).get("agents", [])
        assert any(a["id"] == agent_id for a in agents)

        # Unbind → delete succeeds
        _api(f"/api/v1/agents/{agent_id}", method="delete")
        r = _api(f"/api/v1/downloaders/{dl_id}", method="delete")
        assert r.status_code == 200
        assert _api(f"/api/v1/downloaders/{dl_id}").status_code == 404

        _api(f"/api/v1/channels/{ch_id}", method="delete")

    def test_torrents_unreachable_downloader(self):
        r = _api(
            "/api/v1/downloaders",
            method="post",
            json={
                "name": "Dead Transmission",
                "type": "transmission",
                "url": "http://test-server:1/transmission/rpc",
                "download_dir": "/downloads/dead",
            },
        )
        assert r.status_code == 201
        dl_id = r.json()["data"]["id"]
        try:
            r = _api(f"/api/v1/downloaders/{dl_id}/torrents")
            # Either a clean error status or an empty/error payload — never a hang
            assert r.status_code in (200, 502)
            if r.status_code == 200:
                assert r.json()["success"] is False or r.json()["data"] == []
        finally:
            _api(f"/api/v1/downloaders/{dl_id}", method="delete")

    def test_downloader_404s(self):
        assert _api("/api/v1/downloaders/nonexistent").status_code == 404
        assert _api(
            "/api/v1/downloaders/nonexistent", method="put", json={"name": "x"}
        ).status_code == 404
        assert _api("/api/v1/downloaders/nonexistent", method="delete").status_code == 404


class TestAgentWorksAndValidation:
    def test_work_update(self):
        # Series to subscribe
        r = _api(
            "/api/v1/series",
            method="post",
            json={"title_cn": "作品更新剧集", "title_en": "Work Update Series"},
        )
        assert r.status_code == 201
        series_id = r.json()["data"]["id"]

        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "Work Update Channel",
                "url": GOOD_FEED,
                "field_mapping": DEFAULT_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": False,
            },
        )
        assert r.status_code == 201
        ch_id = r.json()["data"]["id"]

        r = _api("/api/v1/downloaders", params={"page_size": 100})
        dl_id = next(
            (d["id"] for d in r.json()["data"] if d.get("type") == "mock"), None
        )
        if not dl_id:
            r = _api(
                "/api/v1/downloaders",
                method="post",
                json={"name": "Work Update Mock", "type": "mock"},
            )
            assert r.status_code == 201
            dl_id = r.json()["data"]["id"]

        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Work Update Agent",
                "channel_id": ch_id,
                "downloader_id": dl_id,
                "scope_channel_wide": False,
                "llm_enabled": False,
                "works": [{"content_type": "tv", "series_id": series_id}],
            },
        )
        assert r.status_code == 201, f"create agent failed: {r.text}"
        agent = r.json()["data"]
        assert len(agent["works"]) == 1
        work_id = agent["works"][0]["id"]

        # PUT the work: toggle dedup + set filter_overrides
        r = _api(
            f"/api/v1/agents/{agent['id']}/works/{work_id}",
            method="put",
            json={
                "enable_episode_dedup": False,
                "filter_overrides": {
                    "combinator": "and",
                    "conditions": [
                        {"field": "resolution", "operator": "eq", "value": "1080p"},
                    ],
                },
            },
        )
        assert r.status_code == 200, f"work update failed: {r.text}"
        data = r.json()["data"]
        assert data["enable_episode_dedup"] is False
        assert data["filter_overrides"] is not None

        # Cleanup
        _api(f"/api/v1/agents/{agent['id']}", method="delete")
        _api(f"/api/v1/channels/{ch_id}", method="delete")
        _api(f"/api/v1/series/{series_id}", method="delete")

    def test_agent_create_validation(self):
        # Unknown channel → 422
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Bad Agent",
                "channel_id": "nonexistent",
                "downloader_id": "also-nonexistent",
                "scope_channel_wide": True,
            },
        )
        assert r.status_code == 422

        # Unknown downloader (valid channel) → 422
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "Agent Validation Channel",
                "url": GOOD_FEED,
                "field_mapping": DEFAULT_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": False,
            },
        )
        assert r.status_code == 201
        ch_id = r.json()["data"]["id"]
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Bad Agent 2",
                "channel_id": ch_id,
                "downloader_id": "nonexistent",
                "scope_channel_wide": True,
            },
        )
        assert r.status_code == 422
        _api(f"/api/v1/channels/{ch_id}", method="delete")

    def test_agent_404s(self):
        assert _api("/api/v1/agents/nonexistent").status_code == 404
        assert _api(
            "/api/v1/agents/nonexistent", method="put", json={"name": "x"}
        ).status_code == 404
        assert _api("/api/v1/agents/nonexistent", method="delete").status_code == 404
        assert _api(
            "/api/v1/agents/nonexistent/test-filters", method="post", json={}
        ).status_code == 404


class TestChannelNotFound:
    def test_channel_404s(self):
        assert _api("/api/v1/channels/nonexistent").status_code == 404
        assert _api(
            "/api/v1/channels/nonexistent", method="put", json={"name": "x"}
        ).status_code == 404
        assert _api("/api/v1/channels/nonexistent", method="delete").status_code == 404
