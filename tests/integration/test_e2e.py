"""Integration test: end-to-end RSS → parse → agent → download task flow.

Requires Docker Compose with test-server, app, and Transmission running.
"""

import os
import time

import httpx
import pytest

RSSRIPPLE = os.environ.get("RSSRIPPLE_URL", "http://app:9001")
TEST_SERVER = os.environ.get("TEST_SERVER_URL", "http://test-server:8080")
TIMEOUT = 60.0

TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT)


def _api(path: str, method: str = "get", **kw):
    """Convenience HTTP call against the RSSRipple app (with retry)."""
    last_exc = None
    for attempt in range(3):
        try:
            c = _client()
            fn = getattr(c, method.lower())
            return fn(f"{RSSRIPPLE}{path}", **kw)
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            last_exc = e
            time.sleep(1 * (attempt + 1))
    raise last_exc


def _poll_fetch(channel_id: str, timeout: int = 120) -> dict:
    """Poll fetch-status until running → done."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api(f"/api/v1/channels/{channel_id}/fetch-status")
        data = r.json()
        if data.get("data", {}).get("status") == "done":
            return data["data"]
        time.sleep(2)
    raise TimeoutError(f"Fetch did not complete for channel {channel_id}")


class TestE2EFlow:
    """End-to-end integration tests (requires running services)."""

    def test_create_downloader(self):
        """Create and test a Transmission downloader."""
        dl_res = _api("/api/v1/downloaders", method="post", json={
            "name": "E2E Transmission",
            "type": "transmission",
            "url": "http://transmission:9091/transmission/rpc",
            "download_dir": "/downloads",
        })
        assert dl_res.status_code == 201, f"create downloader failed: {dl_res.text}"
        dl_id = dl_res.json()["data"]["id"]

        # Test connection
        test_res = _api(f"/api/v1/downloaders/{dl_id}/test", method="post")
        assert test_res.status_code == 200, f"test connection failed: {test_res.text}"

        TestE2EFlow.downloader_id = dl_id

    def test_create_channel_and_fetch(self):
        """Create channel with dmhy feed and fetch resources."""
        ch_res = _api("/api/v1/channels", method="post", json={
            "name": "E2E Test Feed",
            "url": f"{TEST_SERVER}/rss/dmhy",
            "fetch_interval": 3600,
            "field_mapping": TEST_FIELD_MAPPING,
            "metadata_source": "none",
            "title_extraction_method": "none",
        })
        assert ch_res.status_code == 201, f"create channel failed: {ch_res.text}"
        ch_id = ch_res.json()["data"]["id"]
        TestE2EFlow.channel_id = ch_id

        # Trigger fetch
        fetch_res = _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
        assert fetch_res.status_code == 200

        # Poll for completion
        result = _poll_fetch(ch_id)
        assert result["result"]["new_count"] > 0, f"No resources created: {result}"

        # Verify resources exist
        res_res = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
        assert res_res.status_code == 200
        resources = res_res.json().get("data", [])
        assert len(resources) > 0, "No resources found"
        TestE2EFlow.resource_count = len(resources)

    def test_create_agent_and_run(self):
        """Create agent with filter_config and trigger processing."""
        ag_res = _api("/api/v1/agents", method="post", json={
            "name": "E2E Test Agent",
            "channel_id": TestE2EFlow.channel_id,
            "downloader_id": TestE2EFlow.downloader_id,
            "llm_enabled": False,
            "scope_channel_wide": True,
            "conflict_resolution": "auto",
            "filter_config": {
                "combinator": "and",
                "conditions": [
                    {"field": "resolution", "operator": "eq", "value": "1080p"},
                ],
            },
        })
        assert ag_res.status_code == 201, f"create agent failed: {ag_res.text}"
        ag_id = ag_res.json()["data"]["id"]
        TestE2EFlow.agent_id = ag_id

        # Trigger agent run
        run_res = _api(f"/api/v1/agents/{ag_id}/run", method="post")
        assert run_res.status_code == 200

    def test_cleanup(self):
        """Clean up created resources."""
        try:
            _api(f"/api/v1/agents/{TestE2EFlow.agent_id}", method="delete")
        except Exception:
            pass
        try:
            _api(f"/api/v1/channels/{TestE2EFlow.channel_id}", method="delete")
        except Exception:
            pass
        try:
            _api(f"/api/v1/downloaders/{TestE2EFlow.downloader_id}", method="delete")
        except Exception:
            pass
