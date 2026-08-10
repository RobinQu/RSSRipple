"""Batch (合集) resource dispatch integration tests.

Covers the is_batch branch of agent dispatch: batch resources bypass
per-episode dedup and conflict resolution, but are not dispatched twice
when re-processed (crash-recovery / re-run guard).

Feed: /rss/mikanani-batch — one batch release (01~28 合集) + one single
episode (29) of 葬送的芙莉莲, auto-linked to the pre-seeded Frieren series.

Requirements: Docker test environment with app + test-server services.
"""

from __future__ import annotations

from tests.integration.http._http import (
    RICH_FIELD_MAPPING,
    TEST_SERVER,
    _api,
    _poll_fetch,
    ensure_series,
)

BATCH_FEED_URL = f"{TEST_SERVER}/rss/mikanani-batch"
FRIEREN_TITLE_CN = "葬送的芙莉莲"


def _ensure_mock_downloader() -> str:
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    for d in r.json().get("data", []):
        if d.get("type") == "mock":
            return d["id"]
    r = _api(
        "/api/v1/downloaders",
        method="post",
        json={"name": "Batch Mock Downloader", "type": "mock"},
    )
    assert r.status_code == 201, f"create mock downloader failed: {r.text}"
    return r.json()["data"]["id"]


class TestBatchDispatch:
    def test_batch_resource_dispatched_once(self):
        # Single-season evidence keeps the single-episode resource dispatchable
        # (season-less resources would otherwise go ambiguous → PendingDecision).
        series_id = ensure_series(
            FRIEREN_TITLE_CN, "Frieren: Beyond Journey's End", number_of_seasons=1
        )

        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "Batch Dispatch Channel",
                "url": BATCH_FEED_URL,
                "field_mapping": RICH_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": False,
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.text}"
        ch_id = r.json()["data"]["id"]

        _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
        result = _poll_fetch(ch_id, accept_failed=True)
        assert result.get("status") == "done", f"fetch failed: {result}"

        r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
        resources = r.json().get("data", [])
        assert len(resources) == 2, f"expected 2 resources, got {len(resources)}"

        batch = next((res for res in resources if res.get("is_batch")), None)
        single = next((res for res in resources if not res.get("is_batch")), None)
        assert batch is not None, "expected one batch resource"
        assert single is not None, "expected one single-episode resource"
        # Pre-parser set the batch boundaries and no per-season episode
        assert batch.get("episode") is None
        assert (batch.get("episode_start"), batch.get("episode_end")) == (1, 28)
        # Both auto-linked to the Frieren series
        assert batch.get("series_id") == series_id
        assert single.get("series_id") == series_id
        assert single.get("episode") == 29

        dl_id = _ensure_mock_downloader()
        ids = [res["id"] for res in resources]
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Batch Dispatch Agent",
                "channel_id": ch_id,
                "downloader_id": dl_id,
                "scope_channel_wide": True,
                "llm_enabled": False,
                "conflict_resolution": "auto",
                "dispatch_resource_ids": ids,
            },
        )
        assert r.status_code == 201, f"create agent failed: {r.text}"
        agent_id = r.json()["data"]["id"]

        r = _api(f"/api/v1/agents/{agent_id}/tasks", params={"page_size": 100})
        tasks = r.json()["data"]
        assert len(tasks) == 2, f"batch + single should dispatch 2 tasks, got {len(tasks)}"
        tasked = {t["file_resource_id"] for t in tasks}
        assert tasked == set(ids)

        # Re-process the same resources (rules-preview flow via PUT): the
        # batch dedup guard + episode dedup must skip both — no new tasks.
        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={"dispatch_resource_ids": ids},
        )
        assert r.status_code == 200, f"update with backfill failed: {r.text}"
        r = _api(f"/api/v1/agents/{agent_id}/tasks", params={"page_size": 100})
        assert len(r.json()["data"]) == 2, "re-processing must not duplicate tasks"

        # Cleanup (agent only — channel owns tasks; see other suites)
        _api(f"/api/v1/agents/{agent_id}", method="delete")
