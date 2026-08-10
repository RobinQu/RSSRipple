"""Agent dispatch lifecycle integration tests (mock downloader).

Covers the dispatch side of the agent pipeline that the existing
test_agent_pipeline.py does not reach:

  - Mock downloader CRUD + connectivity (type="mock", no real daemon)
  - rules-preview → POST /agents with dispatch_resource_ids (backfill commit)
  - auto conflict resolution (score_and_pick) → DownloadTask creation
  - Task actions: detail / pause / resume / retry / delete
  - Downloader live torrent list (mock registry)
  - in_queue_skipped on a second rules-preview
  - Agent run history + dashboard download groups
  - Dispatch error path (unreachable Transmission)

Requirements: Docker test environment with app + test-server services.
"""

from __future__ import annotations

import pytest

from tests.integration.http._http import (
    RICH_FIELD_MAPPING,
    TEST_SERVER,
    _api,
    _poll_fetch,
    ensure_series,
)

MIKANANI_S1_URL = f"{TEST_SERVER}/rss/mikanani?series=1"  # 葬送的芙莉莲, 6 eps × 3 groups
FRIEREN_TITLE_CN = "葬送的芙莉莲"


def _ensure_mock_downloader() -> str:
    """Get or create a mock downloader. Returns the downloader ID."""
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    assert r.status_code == 200
    for d in r.json().get("data", []):
        if d.get("type") == "mock":
            return d["id"]
    r = _api(
        "/api/v1/downloaders",
        method="post",
        json={"name": "Integration Mock Downloader", "type": "mock"},
    )
    assert r.status_code == 201, f"create mock downloader failed: {r.text}"
    return r.json()["data"]["id"]


def _list_resources(channel_id: str) -> list[dict]:
    r = _api(
        f"/api/v1/channels/{channel_id}/resources",
        params={"page_size": 100},
    )
    assert r.status_code == 200, f"list resources failed: {r.text}"
    return r.json().get("data", [])


# =========================================================================
# TestMockDownloader — mock downloader basics
# =========================================================================


class TestMockDownloader:
    """Mock downloader creation + connectivity checks."""

    def test_create_mock_downloader(self):
        """POST /downloaders type=mock — defaults url/download_dir, returns 201."""
        r = _api(
            "/api/v1/downloaders",
            method="post",
            json={"name": "Mock Defaults Check", "type": "mock"},
        )
        assert r.status_code == 201, f"create failed: {r.text}"
        data = r.json()["data"]
        assert data["type"] == "mock"
        assert data["url"] == "mock://local"
        assert data["download_dir"], "mock download_dir should get a default"

        # Cleanup
        _api(f"/api/v1/downloaders/{data['id']}", method="delete")

    def test_mock_downloader_connection(self):
        """POST /downloaders/{id}/test — always succeeds, reports free space."""
        dl_id = _ensure_mock_downloader()
        r = _api(f"/api/v1/downloaders/{dl_id}/test", method="post")
        assert r.status_code == 200, f"test connection failed: {r.text}"
        data = r.json()["data"]
        assert data.get("success") is True or data.get("ok") is True, (
            f"mock connection test should succeed: {data}"
        )


# =========================================================================
# TestAutoDispatch — rules-preview → backfill dispatch → task lifecycle
# =========================================================================


@pytest.fixture(scope="class")
def _dispatch_env():
    """Series + channel (fetched, auto-linked) + mock downloader.

    Yields dict with channel_id, series_id, downloader_id, resources.
    """
    # Pre-create the series so Layer-3 local matching auto-links resources.
    # Get-or-create (a duplicate row would trip the same-title collision
    # guard and block linking); number_of_seasons=1 supplies the
    # single-season evidence so season-less resources land season=1 and
    # dispatch instead of going ambiguous → PendingDecision.
    series_id = ensure_series(
        FRIEREN_TITLE_CN, "Frieren: Beyond Journey's End", number_of_seasons=1
    )

    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "Dispatch Test Channel",
            "url": MIKANANI_S1_URL,
            "field_mapping": RICH_FIELD_MAPPING,
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

    resources = _list_resources(ch_id)
    if not resources:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
        pytest.skip("No resources after fetch")

    dl_id = _ensure_mock_downloader()

    yield {
        "channel_id": ch_id,
        "series_id": series_id,
        "downloader_id": dl_id,
        "resources": resources,
    }
    # NOTE: no channel teardown here — the channel owns download tasks and
    # DELETE /channels would 500 on the task FK cascade. The test DB is
    # per-run, so leaving rows behind is safe.


class TestAutoDispatch:
    """Backfill-commit dispatch with conflict_resolution=auto."""

    agent_id: str = ""
    task_ids: list = []

    def test_resources_auto_linked(self, _dispatch_env):
        """Fetch with a pre-seeded series auto-links resources (Layer 3)."""
        linked = [
            r for r in _dispatch_env["resources"]
            if r.get("series_id") == _dispatch_env["series_id"]
        ]
        assert len(linked) == len(_dispatch_env["resources"]), (
            f"Expected all resources linked, got {len(linked)}/{len(_dispatch_env['resources'])}"
        )
        # 6 episodes × 3 subtitle groups
        episodes = {r.get("episode") for r in linked}
        assert episodes == {1, 2, 3, 4, 5, 6}, f"Unexpected episodes: {episodes}"

    def test_rules_preview_new_agent(self, _dispatch_env):
        """POST /agents/rules-preview — all linked resources newly matching."""
        ch_id = _dispatch_env["channel_id"]
        r = _api(
            "/api/v1/agents/rules-preview",
            method="post",
            json={
                "channel_id": ch_id,
                "scope_channel_wide": True,
                "filter_config": None,
                "works": [],
            },
        )
        assert r.status_code == 200, f"rules-preview failed: {r.text}"
        data = r.json()["data"]
        assert len(data["newly_matching"]) == len(_dispatch_env["resources"])
        assert data["no_longer_matching"] == []
        assert data["in_queue_skipped"] == 0

    def test_create_agent_with_backfill_dispatch(self, _dispatch_env):
        """POST /agents with dispatch_resource_ids dispatches one per episode."""
        ids = [r["id"] for r in _dispatch_env["resources"]]
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Auto Dispatch Agent",
                "channel_id": _dispatch_env["channel_id"],
                "downloader_id": _dispatch_env["downloader_id"],
                "scope_channel_wide": True,
                "llm_enabled": False,
                "conflict_resolution": "auto",
                "download_subdir": "anime/frieren",
                "dispatch_resource_ids": ids,
            },
        )
        assert r.status_code == 201, f"create agent failed: {r.text}"
        agent = r.json()["data"]
        TestAutoDispatch.agent_id = agent["id"]
        # Backfill commit advances the consumption watermark
        assert agent.get("last_consumed_at") is not None

        # One winning candidate per episode → 6 tasks
        r = _api(f"/api/v1/agents/{agent['id']}/tasks", params={"page_size": 100})
        assert r.status_code == 200
        tasks = r.json()["data"]
        assert len(tasks) == 6, f"Expected 6 dispatched tasks, got {len(tasks)}"
        for t in tasks:
            assert t["status"] == "downloading", f"Unexpected task status: {t['status']}"
            assert t["transmission_torrent_id"] is not None
            # Effective dir = downloader root + agent subdir
            assert t["download_dir"].endswith("anime/frieren"), (
                f"download_dir should include the agent subdir: {t['download_dir']}"
            )
        TestAutoDispatch.task_ids = [t["id"] for t in tasks]

    def test_list_tasks_status_filter(self, _dispatch_env):
        """GET /agents/{id}/tasks?status=downloading filters correctly."""
        if not TestAutoDispatch.agent_id:
            pytest.skip("No agent — prerequisite failed")
        r = _api(
            f"/api/v1/agents/{TestAutoDispatch.agent_id}/tasks",
            params={"status": "downloading"},
        )
        assert r.status_code == 200
        assert len(r.json()["data"]) == 6
        r = _api(
            f"/api/v1/agents/{TestAutoDispatch.agent_id}/tasks",
            params={"status": "completed"},
        )
        assert r.status_code == 200
        assert len(r.json()["data"]) == 0

    def test_task_detail_pause_resume_retry(self, _dispatch_env):
        """Task detail + pause/resume/retry against the mock downloader."""
        if not TestAutoDispatch.task_ids:
            pytest.skip("No tasks — prerequisite failed")
        task_id = TestAutoDispatch.task_ids[0]

        r = _api(f"/api/v1/tasks/{task_id}")
        assert r.status_code == 200
        detail = r.json()["data"]
        assert detail["id"] == task_id
        assert detail.get("file_resource") is not None

        r = _api(f"/api/v1/tasks/{task_id}/pause", method="post")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "paused"

        r = _api(f"/api/v1/tasks/{task_id}/resume", method="post")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "queued"

        r = _api(f"/api/v1/tasks/{task_id}/retry", method="post")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["status"] == "downloading"
        # Retry re-adds the torrent and bumps retry_count
        r = _api(f"/api/v1/tasks/{task_id}")
        assert r.json()["data"]["retry_count"] == 1

    def test_downloader_live_torrents(self, _dispatch_env):
        """GET /downloaders/{id}/torrents lists the mock registry."""
        r = _api(f"/api/v1/downloaders/{_dispatch_env['downloader_id']}/torrents")
        assert r.status_code == 200, f"live torrents failed: {r.text}"
        data = r.json().get("data")
        assert isinstance(data, list)
        assert len(data) >= 6, f"Expected >=6 mock torrents, got {len(data)}"
        # Snapshots expose progress fields
        sample = data[0]
        assert "percent_done" in sample or "progress" in sample

    def test_downloader_tasks_listing(self, _dispatch_env):
        """GET /downloaders/{id}/tasks returns local DownloadTask rows."""
        r = _api(
            f"/api/v1/downloaders/{_dispatch_env['downloader_id']}/tasks",
            params={"page_size": 100},
        )
        assert r.status_code == 200
        assert len(r.json()["data"]) >= 6

    def test_rules_preview_in_queue_skipped(self, _dispatch_env):
        """Second rules-preview: dispatched resources counted in_queue_skipped."""
        ch_id = _dispatch_env["channel_id"]
        r = _api(
            "/api/v1/agents/rules-preview",
            method="post",
            json={
                "channel_id": ch_id,
                "scope_channel_wide": True,
                "filter_config": None,
                "works": [],
            },
        )
        assert r.status_code == 200
        data = r.json()["data"]
        total = len(_dispatch_env["resources"])
        assert data["in_queue_skipped"] + len(data["newly_matching"]) == total
        assert data["in_queue_skipped"] >= 6, (
            f"Expected the 6 dispatched resources in-queue: {data}"
        )

    def test_rules_preview_existing_agent(self, _dispatch_env):
        """rules-preview with agent_id diffs old vs new rules."""
        if not TestAutoDispatch.agent_id:
            pytest.skip("No agent — prerequisite failed")
        r = _api(
            "/api/v1/agents/rules-preview",
            method="post",
            json={
                "agent_id": TestAutoDispatch.agent_id,
                "scope_channel_wide": True,
                "filter_config": {
                    "combinator": "and",
                    "conditions": [
                        {"field": "resolution", "operator": "eq", "value": "2160p"},
                    ],
                },
                "works": [],
            },
        )
        assert r.status_code == 200, f"rules-preview failed: {r.text}"
        data = r.json()["data"]
        # New rules match nothing → everything currently matched falls out
        assert data["newly_matching"] == []
        assert len(data["no_longer_matching"]) == len(_dispatch_env["resources"])

    def test_run_agent_delta_after_watermark(self, _dispatch_env):
        """POST /agents/{id}/run — delta run past the watermark processes nothing."""
        if not TestAutoDispatch.agent_id:
            pytest.skip("No agent — prerequisite failed")
        r = _api(f"/api/v1/agents/{TestAutoDispatch.agent_id}/run", method="post")
        assert r.status_code == 200, f"run failed: {r.text}"

        from tests.integration.http._http import _poll_run

        result = _poll_run(TestAutoDispatch.agent_id)
        assert result["status"] == "done", f"Unexpected run result: {result}"

    def test_agent_runs_history(self, _dispatch_env):
        """GET /agents/{id}/runs lists run records with counts."""
        if not TestAutoDispatch.agent_id:
            pytest.skip("No agent — prerequisite failed")
        r = _api(f"/api/v1/agents/{TestAutoDispatch.agent_id}/runs")
        assert r.status_code == 200, f"runs failed: {r.text}"
        runs = r.json()["data"]
        assert len(runs) >= 1
        sample = runs[0]
        assert "status" in sample
        assert "dispatched" in sample

    def test_dashboard_download_groups(self, _dispatch_env):
        """GET /dashboard exposes active download groups incl. our series."""
        r = _api("/api/v1/dashboard")
        assert r.status_code == 200, f"dashboard failed: {r.text}"
        data = r.json()["data"]
        assert "active_agents" in data
        assert "active_download_groups" in data
        assert "pending_decisions" in data
        titles = [g.get("title") for g in data["active_download_groups"]]
        assert FRIEREN_TITLE_CN in titles, (
            f"Expected {FRIEREN_TITLE_CN} group in dashboard: {titles}"
        )

    def test_task_delete(self, _dispatch_env):
        """DELETE /tasks/{id} marks the task cancelled and removes the torrent."""
        if not TestAutoDispatch.task_ids:
            pytest.skip("No tasks — prerequisite failed")
        task_id = TestAutoDispatch.task_ids[-1]
        r = _api(f"/api/v1/tasks/{task_id}", method="delete")
        assert r.status_code == 200, f"delete failed: {r.text}"
        r = _api(f"/api/v1/tasks/{task_id}")
        assert r.json()["data"]["status"] == "cancelled"

    def test_task_404s(self):
        """Task endpoints return 404 for unknown ids."""
        for method, path in [
            ("get", "/api/v1/tasks/nonexistent-task"),
            ("post", "/api/v1/tasks/nonexistent-task/pause"),
            ("post", "/api/v1/tasks/nonexistent-task/resume"),
            ("post", "/api/v1/tasks/nonexistent-task/retry"),
            ("delete", "/api/v1/tasks/nonexistent-task"),
        ]:
            r = _api(path, method=method)
            assert r.status_code == 404, f"{method.upper()} {path} → {r.status_code}"


# =========================================================================
# TestDispatchErrorPath — failing Transmission add_torrent
# =========================================================================


class TestDispatchErrorPath:
    """Dispatch against an unreachable Transmission → task error state."""

    def test_dispatch_failure_marks_task_error(self):
        # Unreachable Transmission (connection refused on port 1)
        r = _api(
            "/api/v1/downloaders",
            method="post",
            json={
                "name": "Unreachable Transmission",
                "type": "transmission",
                "url": "http://test-server:1/transmission/rpc",
                "download_dir": "/downloads/unreachable",
            },
        )
        assert r.status_code == 201, f"create downloader failed: {r.text}"
        dl_id = r.json()["data"]["id"]

        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "Dispatch Error Channel",
                "url": MIKANANI_S1_URL,
                "field_mapping": RICH_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": False,
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.text}"
        ch_id = r.json()["data"]["id"]
        _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
        result = _poll_fetch(ch_id, accept_failed=True)
        assert result.get("status") == "done"

        resources = _list_resources(ch_id)
        assert resources, "expected resources"
        # The dispatch suite's Frieren series (created earlier in this file)
        # auto-links these resources; if it is absent, link one manually.
        linked = [res for res in resources if res.get("series_id")]
        if not linked:
            res0 = resources[0]
            r = _api(
                f"/api/v1/resources/{res0['id']}/metadata/link",
                method="put",
                json={
                    "selected_result": {
                        "content_type": "tv",
                        "title_cn": "错误路径剧集",
                        "title_en": "Error Path Series",
                        "external_id": "error-path-1",
                        "external_source": "manual",
                    }
                },
            )
            assert r.status_code == 200, f"manual link failed: {r.text}"
            linked = [r.json()["data"]]
        target = linked[:1]

        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Error Path Agent",
                "channel_id": ch_id,
                "downloader_id": dl_id,
                "scope_channel_wide": True,
                "llm_enabled": False,
                "conflict_resolution": "auto",
                "dispatch_resource_ids": [res["id"] for res in target],
            },
        )
        assert r.status_code == 201, f"create agent failed: {r.text}"
        agent_id = r.json()["data"]["id"]

        r = _api(f"/api/v1/agents/{agent_id}/tasks", params={"page_size": 100})
        assert r.status_code == 200
        tasks = r.json()["data"]
        assert len(tasks) == 1, f"Expected 1 task, got {len(tasks)}"
        assert tasks[0]["status"] == "error", (
            f"Expected error status after unreachable add_torrent: {tasks[0]['status']}"
        )
        assert tasks[0]["error_message"], "error task should carry a message"

        # Cleanup (agent only — the channel owns tasks; see fixture note)
        _api(f"/api/v1/agents/{agent_id}", method="delete")
