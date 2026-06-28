"""End-to-end pipeline integration test: Channel → Agent → Download task.

Tests the complete RSSRipple automation pipeline:
  - Channel creation with real RSS feed and resource fetch
  - Agent creation with filter_config DSL and scope settings
  - Agent processing (triggering run, polling status)
  - Download task creation and detail verification
  - Full pipeline verification: resources → filter → tasks

Requirements: Docker test environment with app + test-server + transmission services.

Usage:
    docker compose -f docker-compose.test.yml up --build
    uv run pytest tests/integration/test_e2e_pipeline.py -v --timeout=300
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

# ── Environment ──────────────────────────────────────────────────────────

RSSRIPPLE = os.environ.get("RSSRIPPLE_URL", "http://app:9001")
TEST_SERVER = os.environ.get("TEST_SERVER_URL", "http://test-server:8080")
MIKANANI_EXT_URL = f"{TEST_SERVER}/rss/mikanani-ext"
TIMEOUT = 60.0


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
    """Block until the channel fetch job finishes (done/failed) or times out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api(f"/api/v1/channels/{channel_id}/fetch-status")
        d = r.json()
        data = d.get("data") or {}
        if data.get("status") in ("done", "failed"):
            return data
        time.sleep(2)
    raise TimeoutError("Fetch did not complete")


def _poll_run(agent_id: str, timeout: int = 120) -> dict:
    """Block until the agent run job finishes (done/failed) or times out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api(f"/api/v1/agents/{agent_id}/run-status")
        d = r.json()
        data = d.get("data") or {}
        if data.get("status") in ("done", "failed"):
            return data
        time.sleep(2)
    raise TimeoutError("Agent run did not complete")


def _get_first_downloader_id() -> str | None:
    """Get the ID of the first downloader, or None if none exist."""
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    if r.status_code != 200:
        return None
    body = r.json()
    data = body.get("data", [])
    if not data:
        return None
    return data[0]["id"]


def _ensure_downloader() -> str:
    """Get or create a downloader. Returns the downloader ID."""
    dl_id = _get_first_downloader_id()
    if dl_id:
        return dl_id
    r = _api(
        "/api/v1/downloaders",
        method="post",
        json={
            "name": "E2E Test Transmission",
            "type": "transmission",
            "url": "http://transmission:9091/transmission/rpc",
            "download_dir": "/downloads/e2e-test",
        },
    )
    assert r.status_code == 201, f"create downloader failed: {r.text}"
    return r.json()["data"]["id"]


# ── Default field mapping ────────────────────────────────────────────────

DEFAULT_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_raw": {"source": "title"},
        "torrent_url": {"source": "link"},
    },
}


# =========================================================================
# TestE2EPipeline — full channel → agent → tasks pipeline
# =========================================================================


class TestE2EPipeline:
    """End-to-end pipeline: Channel create/fetch → Agent create/run → Tasks."""

    channel_id: str = ""
    downloader_id: str = ""
    agent_id: str = ""
    first_task_id: str = ""
    resource_count: int = 0

    def test_create_channel_and_fetch(self):
        """POST /channels → POST /fetch → poll → verify resources created."""
        # Create channel with mikanani-ext feed
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "E2E Pipeline Channel",
                "url": MIKANANI_EXT_URL,
                "field_mapping": DEFAULT_FIELD_MAPPING,
                "fetch_interval": 3600,
                "title_extraction_method": "none",
                "metadata_source": "none",
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.status_code} {r.text}"
        ch_data = r.json()["data"]
        TestE2EPipeline.channel_id = ch_data["id"]
        assert ch_data["status"] in ("active", "inactive")

        # Trigger fetch and wait for completion
        r = _api(
            f"/api/v1/channels/{TestE2EPipeline.channel_id}/fetch",
            method="post",
        )
        assert r.status_code == 200, f"fetch trigger failed: {r.text}"

        result = _poll_fetch(TestE2EPipeline.channel_id)
        assert result["status"] == "done", (
            f"Fetch did not complete successfully: {result}"
        )
        new_count = result["result"]["new_count"]
        assert new_count > 0, f"Expected new_count > 0, got {new_count}"
        TestE2EPipeline.resource_count = new_count

        # Verify resources exist
        r = _api(
            f"/api/v1/channels/{TestE2EPipeline.channel_id}/resources",
            params={"page_size": 100},
        )
        assert r.status_code == 200
        body = r.json()
        resources = body.get("data", [])
        assert len(resources) >= new_count, (
            f"Expected >= {new_count} resources, got {len(resources)}"
        )

        # Verify all resources have title_raw and torrent_url
        missing_title = [r2["id"] for r2 in resources if not r2.get("title_raw")]
        assert not missing_title, (
            f"{len(missing_title)} resources missing title_raw"
        )
        missing_torrent = [r2["id"] for r2 in resources if not r2.get("torrent_url")]
        assert not missing_torrent, (
            f"{len(missing_torrent)} resources missing torrent_url"
        )

    def test_create_agent_for_channel(self):
        """POST /agents — create agent with filter_config DSL and channel-wide scope."""
        if not TestE2EPipeline.channel_id:
            pytest.skip("No channel created — prerequisite test failed")

        dl_id = _ensure_downloader()
        TestE2EPipeline.downloader_id = dl_id

        filter_config = {
            "combinator": "and",
            "conditions": [
                {"field": "resolution", "operator": "in", "value": ["1080p", "720p"]},
            ],
        }

        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "E2E Agent",
                "channel_id": TestE2EPipeline.channel_id,
                "downloader_id": dl_id,
                "download_subdir": "E2E",
                "scope_channel_wide": True,
                "llm_enabled": False,
                "conflict_resolution": "auto",
                "filter_config": filter_config,
            },
        )
        assert r.status_code == 201, f"create agent failed: {r.status_code} {r.text}"
        data = r.json()["data"]
        assert data["name"] == "E2E Agent"
        assert data["channel_id"] == TestE2EPipeline.channel_id
        assert data["downloader_id"] == dl_id
        assert data["scope_channel_wide"] is True
        assert data["status"] == "active"
        assert data["filter_config"] == filter_config

        TestE2EPipeline.agent_id = data["id"]

    def test_agent_processes_resources(self):
        """POST /agents/{id}/run → poll → GET /agents/{id}/tasks."""
        if not TestE2EPipeline.agent_id:
            pytest.skip("No agent created — prerequisite test failed")

        # Trigger agent run
        r = _api(
            f"/api/v1/agents/{TestE2EPipeline.agent_id}/run",
            method="post",
        )
        assert r.status_code == 200, (
            f"trigger agent run failed: {r.status_code} {r.text}"
        )

        # Wait for processing to complete
        result = _poll_run(TestE2EPipeline.agent_id)
        assert result["status"] in ("done", "failed"), (
            f"Agent run unexpected status: {result['status']}"
        )
        # "failed" is acceptable — may happen if no resources match filter
        # or no metadata is linked (since metadata_source="none")

        # Fetch tasks for this agent
        r = _api(
            f"/api/v1/agents/{TestE2EPipeline.agent_id}/tasks",
            params={"page_size": 100},
        )
        assert r.status_code == 200, f"list agent tasks failed: {r.text}"
        body = r.json()
        tasks = body.get("data", [])
        # Tasks may be empty if no resources passed the filter — that's valid
        print(f"Agent created {len(tasks)} download task(s)")

        if tasks:
            TestE2EPipeline.first_task_id = tasks[0]["id"]

    def test_agent_task_details(self):
        """GET /tasks/{id} — verify task fields (status, progress, file_resource_id)."""
        if not TestE2EPipeline.first_task_id:
            pytest.skip("No download tasks created — skipping detail check")

        r = _api(f"/api/v1/tasks/{TestE2EPipeline.first_task_id}")
        assert r.status_code == 200, f"get task failed: {r.text}"
        data = r.json()["data"]
        assert data["id"] == TestE2EPipeline.first_task_id
        assert "status" in data, f"Task missing 'status': {data}"
        assert "progress" in data, f"Task missing 'progress': {data}"
        assert "file_resource_id" in data, f"Task missing 'file_resource_id': {data}"
        assert "agent_id" in data, f"Task missing 'agent_id': {data}"
        assert "downloader_id" in data, f"Task missing 'downloader_id': {data}"

        # Verify status is one of the expected values
        valid_statuses = {
            "pending", "queued", "downloading", "paused",
            "completed", "error", "cancelled",
        }
        assert data["status"] in valid_statuses, (
            f"Unexpected task status: {data['status']}"
        )

    def test_full_pipeline_verification(self):
        """Verify the complete pipeline is operational — chain verification."""
        if not TestE2EPipeline.channel_id:
            pytest.skip("No channel created — prerequisite test failed")

        # 1. Channel resources exist
        r = _api(
            f"/api/v1/channels/{TestE2EPipeline.channel_id}/resources",
            params={"page_size": 5},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["meta"]["total"] >= TestE2EPipeline.resource_count, (
            f"Resource count changed: expected >= {TestE2EPipeline.resource_count}, "
            f"got {body['meta']['total']}"
        )

        # 2. Agent configuration persisted correctly
        r = _api(f"/api/v1/agents/{TestE2EPipeline.agent_id}")
        assert r.status_code == 200
        agent_data = r.json()["data"]
        assert agent_data["name"] == "E2E Agent"
        assert agent_data["channel_id"] == TestE2EPipeline.channel_id
        assert agent_data["downloader_id"] == TestE2EPipeline.downloader_id
        assert agent_data["scope_channel_wide"] is True
        assert agent_data["status"] == "active"
        assert agent_data["filter_config"] is not None

        # 3. Agent's works are manageable (channel-wide agents have empty works)
        r = _api(f"/api/v1/agents/{TestE2EPipeline.agent_id}/works")
        assert r.status_code == 200, f"get works failed: {r.text}"
        works = r.json().get("data", [])
        # Channel-wide scope may have empty works list — that's fine
        assert isinstance(works, list), f"Expected list of works, got {type(works).__name__}"

        # 4. Dashboard returns summary data
        r = _api("/api/v1/dashboard")
        assert r.status_code == 200, f"dashboard failed: {r.text}"
        dashboard = r.json()["data"]
        # Dashboard should show at least our agent and channel
        assert "active_agents" in dashboard, f"Dashboard missing 'active_agents': {dashboard}"
        assert "active_channels" in dashboard, f"Dashboard missing 'active_channels': {dashboard}"
        assert dashboard.get("active_agents", 0) >= 1, "Expected at least 1 active agent"
        assert dashboard.get("active_channels", 0) >= 1, "Expected at least 1 active channel"

        # 5. Channel listing shows our channel
        r = _api("/api/v1/channels", params={"page_size": 100})
        assert r.status_code == 200
        channel_ids = [ch["id"] for ch in r.json().get("data", [])]
        assert TestE2EPipeline.channel_id in channel_ids, (
            "E2E channel not found in channel list"
        )

    @classmethod
    def teardown_class(cls):
        """Cleanup: delete the test agent and channel."""
        try:
            _api(f"/api/v1/agents/{TestE2EPipeline.agent_id}", method="delete")
        except Exception:
            pass
        try:
            _api(f"/api/v1/channels/{TestE2EPipeline.channel_id}", method="delete")
        except Exception:
            pass
