"""DownloadTask API surface integration coverage.

The focused suites reach task detail/pause/resume/retry/delete on agent-owned
tasks (test_agent_dispatch.py). This file exercises the remaining
``app/api/v1/tasks.py`` paths:

  - global GET /tasks with downloader/agent/status filters + invalid-status 422
  - GET /tasks/{id} 404
  - POST /tasks manual creation (resource/downloader 404s + success)
  - task action 404s (pause/resume/retry)
  - POST /agents/{id}/tasks/batch-retry (scoped task_ids + error path)
  - DELETE /tasks/{id} 404 + delete_data=True

Requirements: Docker test environment (app + test-server).
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

MIKANANI_S1_URL = f"{TEST_SERVER}/rss/mikanani?series=1"
FRIEREN_TITLE_CN = "葬送的芙莉莲"


def _ensure_mock_downloader() -> str:
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    assert r.status_code == 200
    for d in r.json().get("data", []):
        if d.get("type") == "mock":
            return d["id"]
    r = _api(
        "/api/v1/downloaders",
        method="post",
        json={"name": "Tasks API Mock Downloader", "type": "mock"},
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


@pytest.fixture(scope="class")
def _task_env():
    """Series + fetched/auto-linked channel + mock downloader + dispatched agent."""
    series_id = ensure_series(
        FRIEREN_TITLE_CN, "Frieren: Beyond Journey's End", number_of_seasons=1
    )
    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "Tasks API Channel",
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
        pytest.skip(f"Fetch did not complete: {result}")
    resources = _list_resources(ch_id)
    if not resources:
        pytest.skip("No resources after fetch")
    dl_id = _ensure_mock_downloader()

    r = _api(
        "/api/v1/agents",
        method="post",
        json={
            "name": "Tasks API Agent",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "scope_channel_wide": True,
            "llm_enabled": False,
            "conflict_resolution": "auto",
            "dispatch_resource_ids": [resources[0]["id"]],
        },
    )
    assert r.status_code == 201, f"create agent failed: {r.text}"
    agent_id = r.json()["data"]["id"]

    r = _api(f"/api/v1/agents/{agent_id}/tasks", params={"page_size": 100})
    tasks = r.json()["data"]
    assert tasks, "no tasks after dispatch"

    yield {
        "channel_id": ch_id,
        "series_id": series_id,
        "downloader_id": dl_id,
        "agent_id": agent_id,
        "resources": resources,
        "task": tasks[0],
    }
    # Channel owns tasks (FK cascade) — leave rows for the per-run DB.


class TestTaskListing:
    def test_global_list_filters(self, _task_env):
        r = _api("/api/v1/tasks", params={"page_size": 100})
        assert r.status_code == 200
        assert len(r.json()["data"]) >= 1

        r = _api(
            "/api/v1/tasks",
            params={"downloader_id": _task_env["downloader_id"]},
        )
        assert r.status_code == 200
        assert len(r.json()["data"]) >= 1

        r = _api(
            "/api/v1/tasks",
            params={"agent_id": _task_env["agent_id"], "status": "downloading"},
        )
        assert r.status_code == 200
        assert {t["id"] for t in r.json()["data"]} == {_task_env["task"]["id"]}

    def test_global_list_invalid_status_422(self, _task_env):
        r = _api("/api/v1/tasks", params={"status": "not-a-status"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_task_404(self, _task_env):
        r = _api("/api/v1/tasks/no-such-task")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"


class TestTaskActions:
    def test_action_404s(self, _task_env):
        for action in ("pause", "resume", "retry"):
            r = _api(f"/api/v1/tasks/no-such-task/{action}", method="post")
            assert r.status_code == 404, f"{action} should 404"

    def test_pause_resume_delete_flow(self, _task_env):
        task_id = _task_env["task"]["id"]
        # Retry on the mock downloader re-adds the torrent.
        r = _api(f"/api/v1/tasks/{task_id}/retry", method="post")
        assert r.status_code == 200, f"retry failed: {r.text}"
        assert r.json()["data"]["status"] == "downloading"
        assert r.json()["data"]["message"] in ("retried", "failed")

        # delete with delete_data=True marks the task cancelled and removes
        # the torrent (the row itself is retained for audit).
        r = _api(f"/api/v1/tasks/{task_id}", method="delete", params={"delete_data": "true"})
        assert r.status_code == 200, f"delete with data failed: {r.text}"
        assert r.json()["data"]["deleted"] is True
        r = _api(f"/api/v1/tasks/{task_id}")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "cancelled"

        # delete without delete_data shares task_cleanup (organize semantics):
        # the row is marked cancelled, not removed.
        r = _api(f"/api/v1/tasks/{task_id}", method="delete")
        assert r.status_code == 200
        r = _api(f"/api/v1/tasks/{task_id}")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "cancelled"

    def test_delete_404(self, _task_env):
        r = _api("/api/v1/tasks/no-such-task", method="delete")
        assert r.status_code == 404


class TestManualTaskCreate:
    def test_create_missing_resource_404(self, _task_env):
        r = _api(
            "/api/v1/tasks",
            method="post",
            json={"resource_id": "no-such-resource", "downloader_id": _task_env["downloader_id"]},
        )
        assert r.status_code == 404

    def test_create_missing_downloader_404(self, _task_env):
        r = _api(
            "/api/v1/tasks",
            method="post",
            json={"resource_id": _task_env["resources"][0]["id"], "downloader_id": "no-such-dl"},
        )
        assert r.status_code == 404

    def test_create_manual_task_success(self, _task_env):
        r = _api(
            "/api/v1/tasks",
            method="post",
            json={"resource_id": _task_env["resources"][0]["id"],
                  "downloader_id": _task_env["downloader_id"]},
        )
        assert r.status_code == 201, f"manual create failed: {r.text}"
        task = r.json()["data"]
        assert task["status"] == "downloading"
        assert task["transmission_torrent_id"] is not None
        assert task["agent_id"] is None
        # Cleanup.
        _api(f"/api/v1/tasks/{task['id']}", method="delete")


class TestBatchRetry:
    def test_batch_retry_scoped_and_errors(self, _task_env):
        # batch-retry only processes error/paused tasks — pause first so the
        # task is in scope.
        task_id = _task_env["task"]["id"]
        r = _api(f"/api/v1/tasks/{task_id}/pause", method="post")
        assert r.status_code == 200, f"pause failed: {r.text}"

        r = _api(
            f"/api/v1/agents/{_task_env['agent_id']}/tasks/batch-retry",
            method="post",
            json={"task_ids": [task_id]},
        )
        assert r.status_code == 200, f"batch retry failed: {r.text}"
        data = r.json()["data"]
        assert data["processed"] == 1
        assert data["retried"] + data["failed"] == 1

        # Foreign agent → nothing processed.
        r = _api(
            "/api/v1/agents/no-such-agent/tasks/batch-retry",
            method="post",
            json={"task_ids": []},
        )
        assert r.status_code == 200
        assert r.json()["data"]["processed"] == 0
