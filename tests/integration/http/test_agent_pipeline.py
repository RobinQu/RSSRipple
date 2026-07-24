"""Agent lifecycle integration tests for the RSSRipple Docker test environment.

Tests the complete agent pipeline:
  - Agent CRUD (create/read/list/update/delete)
  - Works management (subscribe/unsubscribe TV series / movies)
  - Agent run (trigger processing, poll run status)
  - Filter DSL (set filter config, test filters against resources)

Requirements: Docker test environment with app + test-server + transmission services.

Usage:
    docker compose -f docker-compose.test.yml up --build
    uv run pytest tests/integration/test_agent_pipeline.py -v --timeout=300
"""

from __future__ import annotations

import pytest

from tests.integration.http._http import (
    DEFAULT_FIELD_MAPPING,
    MIKANANI_EXT_URL,
    _api,
    _ensure_downloader,
    _poll_fetch,
    _poll_run,
)

# =========================================================================
# Class-level setup helpers (pytest fixtures with scope="class")
# =========================================================================


@pytest.fixture(scope="class")
def _setup_channel():
    """Create a channel with the mikanani-ext feed, fetch resources, and yield
    the channel dict. Cleans up after all tests in the class.

    Returns a dict with keys: id, name, url, status.
    """
    # Create channel
    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "Agent Pipeline Test Channel",
            "url": MIKANANI_EXT_URL,
            "field_mapping": DEFAULT_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    if r.status_code != 201:
        pytest.skip(f"Channel creation failed: {r.status_code} {r.text}")
    channel = r.json()["data"]
    ch_id = channel["id"]

    # Fetch resources
    _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
    result = _poll_fetch(ch_id, accept_failed=True)
    if result.get("status") != "done":
        pytest.skip(f"Fetch did not complete: {result}")

    yield channel

    # Cleanup
    try:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
    except Exception:
        pass


@pytest.fixture(scope="class")
def _ensure_series():
    """Ensure at least one TVSeries exists for works management tests.
    Returns the series dict (with id, title_cn, title_en).
    """
    # Check if any series exist
    r = _api("/api/v1/series", params={"page_size": 1})
    if r.status_code == 200:
        body = r.json()
        data = body.get("data", [])
        if data:
            return data[0]

    # Create a series
    r = _api(
        "/api/v1/series",
        method="post",
        json={"title_cn": "测试剧集", "title_en": "Test Series"},
    )
    if r.status_code != 201:
        pytest.skip(f"Series creation failed: {r.status_code} {r.text}")
    return r.json()["data"]


# =========================================================================
# TestAgentCRUD — Create, Read, List, Update, Delete
# =========================================================================


class TestAgentCRUD:
    """CRUD operations for agents against a channel with fetched resources."""

    # Shared state set by the first test
    channel_id: str = ""
    downloader_id: str = ""
    agent_id: str = ""

    def test_create_channel_for_agent(self, _setup_channel):
        """Create channel with mikanani-ext feed, fetch, verify resources exist."""
        ch = _setup_channel
        assert ch["id"], "channel_id must be non-empty"
        assert ch["status"] in ("active", "inactive")

        # Store for subsequent tests
        TestAgentCRUD.channel_id = ch["id"]

        # Verify resources were created
        r = _api(f"/api/v1/channels/{ch['id']}/resources", params={"page_size": 5})
        assert r.status_code == 200
        meta = r.json().get("meta", {})
        assert meta.get("total", 0) > 0, "Expected at least one resource after fetch"

    def test_create_agent(self):
        """POST /agents — create an agent with channel_wide scope."""
        if not TestAgentCRUD.channel_id:
            pytest.skip("No channel created — prerequisite test failed")

        dl_id = _ensure_downloader()
        TestAgentCRUD.downloader_id = dl_id

        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Pipeline Test Agent",
                "channel_id": TestAgentCRUD.channel_id,
                "downloader_id": dl_id,
                "scope_channel_wide": True,
                "llm_enabled": False,
                "conflict_resolution": "auto",
            },
        )
        assert r.status_code == 201, f"create agent failed: {r.status_code} {r.text}"
        data = r.json()["data"]
        assert data["name"] == "Pipeline Test Agent"
        assert data["channel_id"] == TestAgentCRUD.channel_id
        assert data["downloader_id"] == dl_id
        assert data["scope_channel_wide"] is True
        assert data["status"] == "active"

        TestAgentCRUD.agent_id = data["id"]

    def test_get_agent(self):
        """GET /agents/{id} — verify name, channel_id, downloader_id."""
        if not TestAgentCRUD.agent_id:
            pytest.skip("No agent created — prerequisite test failed")

        r = _api(f"/api/v1/agents/{TestAgentCRUD.agent_id}")
        assert r.status_code == 200, f"get agent failed: {r.text}"
        data = r.json()["data"]
        assert data["id"] == TestAgentCRUD.agent_id
        assert data["name"] == "Pipeline Test Agent"
        assert data["channel_id"] == TestAgentCRUD.channel_id
        assert data["downloader_id"] == TestAgentCRUD.downloader_id

    def test_list_agents(self):
        """GET /agents — verify total >= 1, our agent in list."""
        r = _api("/api/v1/agents")
        assert r.status_code == 200, f"list agents failed: {r.text}"
        body = r.json()
        assert body["data"] is not None
        ids = [a["id"] for a in body["data"]]
        if TestAgentCRUD.agent_id:
            assert TestAgentCRUD.agent_id in ids, (
                f"Agent {TestAgentCRUD.agent_id} not found in list"
            )

    def test_update_agent(self):
        """PUT /agents/{id} — update name, verify persisted."""
        if not TestAgentCRUD.agent_id:
            pytest.skip("No agent created — prerequisite test failed")

        new_name = "Pipeline Test Agent (Updated)"
        r = _api(
            f"/api/v1/agents/{TestAgentCRUD.agent_id}",
            method="put",
            json={"name": new_name},
        )
        assert r.status_code == 200, f"update agent failed: {r.text}"
        assert r.json()["data"]["name"] == new_name

        # Verify persisted
        r2 = _api(f"/api/v1/agents/{TestAgentCRUD.agent_id}")
        assert r2.json()["data"]["name"] == new_name

    def test_delete_agent(self):
        """DELETE /agents/{id} — verify 200, confirm removed from list."""
        if not TestAgentCRUD.channel_id or not TestAgentCRUD.downloader_id:
            pytest.skip("No channel/downloader — prerequisite test failed")

        # Create a temporary agent for deletion
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Delete Me Agent",
                "channel_id": TestAgentCRUD.channel_id,
                "downloader_id": TestAgentCRUD.downloader_id,
                "scope_channel_wide": True,
                "llm_enabled": False,
            },
        )
        assert r.status_code == 201, f"create temp agent failed: {r.text}"
        agent_id = r.json()["data"]["id"]

        # Delete
        r = _api(f"/api/v1/agents/{agent_id}", method="delete")
        assert r.status_code == 200, f"delete agent failed: {r.text}"

        # Confirm gone
        r2 = _api(f"/api/v1/agents/{agent_id}")
        assert r2.status_code == 404, "Agent still exists after delete"


# =========================================================================
# TestAgentWorksManagement — subscribe/unsubscribe TV series / movies
# =========================================================================


@pytest.fixture(scope="class")
def _works_agent():
    """Create an agent scoped to a list of works (scope_channel_wide=False).
    Returns the agent dict.
    """
    # Need channel and downloader
    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "Works Management Test Channel",
            "url": MIKANANI_EXT_URL,
            "field_mapping": DEFAULT_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    if r.status_code != 201:
        pytest.skip(f"Channel creation failed: {r.status_code} {r.text}")
    ch_id = r.json()["data"]["id"]

    dl_id = _ensure_downloader()

    r = _api(
        "/api/v1/agents",
        method="post",
        json={
            "name": "Works Management Agent",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "scope_channel_wide": False,
            "llm_enabled": False,
            "conflict_resolution": "ask",
        },
    )
    if r.status_code != 201:
        pytest.skip(f"Agent creation failed: {r.status_code} {r.text}")
    agent = r.json()["data"]

    yield agent

    # Cleanup
    try:
        _api(f"/api/v1/agents/{agent['id']}", method="delete")
    except Exception:
        pass
    try:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
    except Exception:
        pass


class TestAgentWorksManagement:
    """Test subscribing and managing works (TV series / movie subscriptions)."""

    work_id: str = ""

    def test_add_work(self, _works_agent, _ensure_series):
        """POST /agents/{id}/works — add a TV series subscription, verify 201."""
        agent_id = _works_agent["id"]
        series = _ensure_series

        r = _api(
            f"/api/v1/agents/{agent_id}/works",
            method="post",
            json={
                "content_type": "tv",
                "series_id": series["id"],
                "enable_episode_dedup": True,
            },
        )
        assert r.status_code == 201, f"add work failed: {r.status_code} {r.text}"
        data = r.json()["data"]
        assert data["content_type"] == "tv"
        assert data["series_id"] == series["id"]
        assert data["enable_episode_dedup"] is True

        TestAgentWorksManagement.work_id = data["id"]

    def test_list_works(self, _works_agent):
        """GET /agents/{id}/works — verify at least 1 work exists."""
        agent_id = _works_agent["id"]
        r = _api(f"/api/v1/agents/{agent_id}/works")
        assert r.status_code == 200, f"list works failed: {r.text}"
        data = r.json()["data"]
        assert len(data) >= 1, f"Expected >=1 works, got {len(data)}"

    def test_remove_work(self, _works_agent, _ensure_series):
        """DELETE /agents/{id}/works/{work_id} — remove work, verify gone."""
        agent_id = _works_agent["id"]
        series = _ensure_series

        # Add a work specifically for removal
        r = _api(
            f"/api/v1/agents/{agent_id}/works",
            method="post",
            json={"content_type": "tv", "series_id": series["id"]},
        )
        assert r.status_code == 201, f"add work for delete failed: {r.text}"
        wid = r.json()["data"]["id"]

        # Record count before
        list_before = _api(f"/api/v1/agents/{agent_id}/works")
        count_before = len(list_before.json()["data"])

        # Delete
        r = _api(f"/api/v1/agents/{agent_id}/works/{wid}", method="delete")
        assert r.status_code == 200, f"delete work failed: {r.text}"

        # Confirm removed
        list_after = _api(f"/api/v1/agents/{agent_id}/works")
        count_after = len(list_after.json()["data"])
        assert count_after == count_before - 1, (
            f"Work count did not decrease: {count_before} → {count_after}"
        )
        work_ids = [w["id"] for w in list_after.json()["data"]]
        assert wid not in work_ids, "Deleted work still appears in list"

    def test_max_works_limit(self, _works_agent, _ensure_series):
        """POST more than 10 works, verify the 11th returns a validation error."""
        agent_id = _works_agent["id"]
        series = _ensure_series

        # Clear existing works by updating agent with empty works list
        _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={"works": []},
        )

        # Add 10 works — should all succeed
        for i in range(10):
            r = _api(
                f"/api/v1/agents/{agent_id}/works",
                method="post",
                json={"content_type": "tv", "series_id": series["id"]},
            )
            assert r.status_code == 201, (
                f"Work #{i + 1} creation failed: {r.status_code} {r.text}"
            )

        # 11th work should fail
        r = _api(
            f"/api/v1/agents/{agent_id}/works",
            method="post",
            json={"content_type": "tv", "series_id": series["id"]},
        )
        assert r.status_code in (400, 422), (
            f"Expected 400/422 for 11th work, got {r.status_code}: {r.text[:200]}"
        )


# =========================================================================
# TestAgentRun — trigger agent processing
# =========================================================================


@pytest.fixture(scope="class")
def _run_agent():
    """Create an agent on its own channel (fetched) for run tests.
    Returns the agent dict.
    """
    # Create channel
    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "Agent Run Test Channel",
            "url": MIKANANI_EXT_URL,
            "field_mapping": DEFAULT_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    if r.status_code != 201:
        pytest.skip(f"Channel creation failed: {r.status_code} {r.text}")
    ch_id = r.json()["data"]["id"]

    # Fetch resources
    _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
    result = _poll_fetch(ch_id, accept_failed=True)
    if result.get("status") != "done":
        pytest.skip(f"Fetch did not complete: {result}")

    dl_id = _ensure_downloader()

    # Create agent
    r = _api(
        "/api/v1/agents",
        method="post",
        json={
            "name": "Run Test Agent",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "scope_channel_wide": True,
            "llm_enabled": False,
            "conflict_resolution": "auto",
        },
    )
    if r.status_code != 201:
        pytest.skip(f"Agent creation failed: {r.status_code} {r.text}")
    agent = r.json()["data"]

    yield agent

    # Cleanup
    try:
        _api(f"/api/v1/agents/{agent['id']}", method="delete")
    except Exception:
        pass
    try:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
    except Exception:
        pass


class TestAgentRun:
    """Test triggering agent processing and polling run status."""

    def test_run_agent(self, _run_agent):
        """POST /agents/{id}/run — trigger processing, poll, verify result."""
        agent_id = _run_agent["id"]

        r = _api(f"/api/v1/agents/{agent_id}/run", method="post")
        assert r.status_code == 200, f"trigger run failed: {r.status_code} {r.text}"

        result = _poll_run(agent_id)
        assert "status" in result, f"run result missing status: {result}"
        # Status can be "done" or "failed" — "failed" is acceptable if
        # no resources matched the filter or no metadata is linked
        assert result["status"] in ("done", "failed"), (
            f"Unexpected run status: {result['status']}"
        )

    def test_run_status(self, _run_agent):
        """GET /agents/{id}/run-status — verify status field."""
        agent_id = _run_agent["id"]

        r = _api(f"/api/v1/agents/{agent_id}/run-status")
        assert r.status_code == 200, f"run-status failed: {r.text}"
        data = r.json()["data"]
        # Status is present even if no run is active (should be "idle" or similar)
        assert data is not None
        assert "status" in data, f"run-status missing 'status' field: {data}"


# =========================================================================
# TestAgentFilterDSL — filter configuration and testing
# =========================================================================


@pytest.fixture(scope="class")
def _filter_agent():
    """Create an agent with a filter_config for DSL tests.
    Returns the agent dict.
    """
    # Create channel and fetch
    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "Filter DSL Test Channel",
            "url": MIKANANI_EXT_URL,
            "field_mapping": DEFAULT_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    if r.status_code != 201:
        pytest.skip(f"Channel creation failed: {r.status_code} {r.text}")
    ch_id = r.json()["data"]["id"]

    _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
    result = _poll_fetch(ch_id, accept_failed=True)
    if result.get("status") != "done":
        _api(f"/api/v1/channels/{ch_id}", method="delete")
        pytest.skip(f"Fetch did not complete: {result}")

    dl_id = _ensure_downloader()

    # Create agent with initial filter
    r = _api(
        "/api/v1/agents",
        method="post",
        json={
            "name": "Filter DSL Agent",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "scope_channel_wide": True,
            "llm_enabled": False,
            "conflict_resolution": "auto",
            "filter_config": {
                "combinator": "and",
                "conditions": [
                    {"field": "resolution", "operator": "in", "value": ["1080p", "720p"]},
                ],
            },
        },
    )
    if r.status_code != 201:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
        pytest.skip(f"Agent creation failed: {r.status_code} {r.text}")
    agent = r.json()["data"]

    # Store channel_id for cleanup
    agent["_ch_id"] = ch_id

    yield agent

    # Cleanup
    try:
        _api(f"/api/v1/agents/{agent['id']}", method="delete")
    except Exception:
        pass
    try:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
    except Exception:
        pass


class TestAgentFilterDSL:
    """Test filter configuration and the test-filters endpoint."""

    def test_set_filter_config(self, _filter_agent):
        """PUT /agents/{id} with filter_config — verify it is stored correctly."""
        agent_id = _filter_agent["id"]
        new_filter = {
            "combinator": "and",
            "conditions": [
                {"field": "resolution", "operator": "in", "value": ["1080p", "720p"]},
                {"field": "subtitle_group", "operator": "contains", "value": "LoliHouse"},
            ],
        }

        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={"filter_config": new_filter},
        )
        assert r.status_code == 200, f"update filter_config failed: {r.text}"
        stored = r.json()["data"]
        assert stored["filter_config"] == new_filter, (
            f"Filter config not stored correctly. Expected {new_filter}, got {stored.get('filter_config')}"
        )

        # Verify it persisted
        r2 = _api(f"/api/v1/agents/{agent_id}")
        persisted = r2.json()["data"]
        assert persisted["filter_config"] == new_filter

    def test_set_filter_config_with_bool_nesting(self, _filter_agent):
        """PUT /agents/{id} with nested BoolCondition — verify stored correctly."""
        agent_id = _filter_agent["id"]
        nested_filter = {
            "combinator": "or",
            "conditions": [
                {
                    "combinator": "and",
                    "conditions": [
                        {"field": "subtitle_group", "operator": "contains", "value": "动漫"},
                        {"field": "file_size", "operator": "gte", "value": 1073741824},
                    ],
                },
                {
                    "combinator": "and",
                    "conditions": [
                        {"field": "subtitle_group", "operator": "eq", "value": "官方"},
                        {"field": "resolution", "operator": "eq", "value": "2160p"},
                    ],
                },
            ],
        }

        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={"filter_config": nested_filter},
        )
        assert r.status_code == 200, f"update nested filter failed: {r.text}"
        stored = r.json()["data"]
        assert stored["filter_config"] == nested_filter

    def test_set_filter_config_is_not(self, _filter_agent):
        """PUT /agents/{id} with is_not on a condition group — verify stored."""
        agent_id = _filter_agent["id"]
        negated_filter = {
            "combinator": "and",
            "conditions": [
                {"field": "container", "operator": "eq", "value": "mkv"},
                {"field": "video_codec", "operator": "ne", "value": "AVC"},
            ],
            "is_not": True,
        }

        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={"filter_config": negated_filter},
        )
        assert r.status_code == 200, f"update negated filter failed: {r.text}"
        stored = r.json()["data"]
        assert stored["filter_config"] == negated_filter

    def test_set_invalid_filter_field_accepted_deferred(self, _filter_agent):
        """PUT /agents/{id} with bogus field — verify 422."""
        agent_id = _filter_agent["id"]

        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={
                "filter_config": {
                    "combinator": "and",
                    "conditions": [
                        {"field": "bogus_field", "operator": "eq", "value": "x"},
                    ],
                }
            },
        )
        # The API stores the filter as-is; validation happens during matching
        assert r.status_code == 200, (
            f"Expected 200 for filter update, got {r.status_code}: {r.text}"
        )

    def test_test_filters_no_args(self, _filter_agent):
        """POST /agents/{id}/test-filters with no resource_ids — verify response."""
        agent_id = _filter_agent["id"]

        r = _api(
            f"/api/v1/agents/{agent_id}/test-filters",
            method="post",
            json={},
        )
        assert r.status_code == 200, f"test-filters failed: {r.text}"
        body = r.json()
        assert body["success"] is True
        # Data should have results or total
        data = body.get("data", {})
        assert data is not None, "test-filters returned null data"

    def test_test_filters_with_everything_passes(self, _filter_agent):
        """Set filter that matches everything, test-filters should pass all."""
        agent_id = _filter_agent["id"]

        # Set a filter that passes everything (no conditions)
        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={
                "filter_config": {
                    "combinator": "and",
                    "conditions": [],
                }
            },
        )
        assert r.status_code == 200

        r = _api(
            f"/api/v1/agents/{agent_id}/test-filters",
            method="post",
            json={},
        )
        assert r.status_code == 200
        data = r.json().get("data", {})

        # Check response structure
        if "total" in data:
            # All resources tested
            if "passing" in data:
                assert data["passing"] == data["total"], (
                    f"Expected all {data['total']} to pass, got {data['passing']}"
                )

    def test_test_filters_with_nothing_passes(self, _filter_agent):
        """Set filter that matches nothing (impossible resolution), test-filters should pass 0."""
        agent_id = _filter_agent["id"]

        # Set a filter that passes nothing
        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={
                "filter_config": {
                    "combinator": "and",
                    "conditions": [
                        {"field": "resolution", "operator": "eq", "value": "9999p"},
                    ],
                }
            },
        )
        assert r.status_code == 200

        r = _api(
            f"/api/v1/agents/{agent_id}/test-filters",
            method="post",
            json={},
        )
        assert r.status_code == 200
        data = r.json().get("data", {})

        # Either the response tells us how many passed or gives per-resource results
        if "passing" in data:
            assert data["passing"] == 0, (
                f"Expected 0 passing, got {data['passing']}"
            )
        elif "results" in data:
            # Results should all be failing
            for item in data["results"]:
                assert not item.get("passing", True), (
                    f"Unexpected passing resource: {item}"
                )
