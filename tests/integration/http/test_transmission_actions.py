"""Real-Transmission RPC integration tests.

Exercises TransmissionWrapper's live RPC surface (add/pause/resume/remove,
torrent listing) end-to-end: an agent dispatches real torrents (served by
the test-server) to the transmission service, and task actions go through
the actual daemon.

Skips gracefully when the transmission service is unreachable (it is
started via test-runner's depends_on in the standard compose flow).

Requirements: Docker test environment with app + test-server + transmission.
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

# dmhy-style feed: magnet links (transmission can add magnets without
# fetching a .torrent file; the test-server seeds the real payloads).
# extract_search_title reduces the titles to 黄泉使者.
FEED_URL = f"{TEST_SERVER}/rss/dmhy?series=0"
SERIES_TITLE_CN = "黄泉使者"


@pytest.fixture(scope="class")
def _tx_env():
    """Agent that dispatched real torrents to the transmission service."""
    series_id = ensure_series(
        SERIES_TITLE_CN, "Daemons of the Shadow Realm", number_of_seasons=1
    )
    # Dedicated downloader at the compose transmission port — do NOT reuse
    # _ensure_downloader (other suites register unreachable downloaders and
    # the helper just returns the first row).
    dl_id = None
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    for d in r.json().get("data", []):
        if d.get("type") == "transmission" and ":9091" in d.get("url", ""):
            dl_id = d["id"]
            break
    if not dl_id:
        r = _api(
            "/api/v1/downloaders",
            method="post",
            json={
                "name": "Transmission Actions Downloader",
                "type": "transmission",
                "url": "http://transmission:9091/transmission/rpc",
                "download_dir": "/downloads/complete",
            },
        )
        if r.status_code != 201:
            pytest.skip(f"downloader setup failed: {r.text}")
        dl_id = r.json()["data"]["id"]

    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "Transmission Actions Channel",
            "url": FEED_URL,
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

    r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
    linked = [res for res in r.json().get("data", []) if res.get("series_id") == series_id]
    if not linked:
        pytest.skip("resources not linked")

    r = _api(
        "/api/v1/agents",
        method="post",
        json={
            "name": "Transmission Actions Agent",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "scope_channel_wide": True,
            "llm_enabled": False,
            "conflict_resolution": "auto",
            "dispatch_resource_ids": [res["id"] for res in linked],
        },
    )
    if r.status_code != 201:
        pytest.skip(f"Agent creation failed: {r.status_code} {r.text}")
    agent_id = r.json()["data"]["id"]

    r = _api(f"/api/v1/agents/{agent_id}/tasks", params={"page_size": 100})
    tasks = r.json().get("data", [])
    downloading = [t for t in tasks if t["status"] == "downloading"]
    if not downloading:
        pytest.skip(
            f"transmission unreachable or add_torrent failed: "
            f"{[(t['status'], t.get('error_message')) for t in tasks][:2]}"
        )

    yield {"agent_id": agent_id, "downloader_id": dl_id, "tasks": downloading}
    # Channel owns tasks → leave it (per-run DB).


class TestTransmissionActions:
    def test_live_torrent_list(self, _tx_env):
        """GET /downloaders/{id}/torrents hits the real daemon."""
        r = _api(f"/api/v1/downloaders/{_tx_env['downloader_id']}/torrents")
        assert r.status_code == 200, f"torrents failed: {r.text}"
        data = r.json().get("data")
        assert isinstance(data, list)
        assert len(data) >= len(_tx_env["tasks"]), (
            f"expected >= {len(_tx_env['tasks'])} torrents on the daemon"
        )

    def test_pause_resume_delete(self, _tx_env):
        """pause/resume/delete go through the real RPC."""
        task = _tx_env["tasks"][0]
        tid = task["id"]

        r = _api(f"/api/v1/tasks/{tid}/pause", method="post")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "paused"

        r = _api(f"/api/v1/tasks/{tid}/resume", method="post")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "queued"

        r = _api(f"/api/v1/tasks/{tid}", method="delete", params={"delete_data": True})
        assert r.status_code == 200, f"delete failed: {r.text}"
        r = _api(f"/api/v1/tasks/{tid}")
        assert r.json()["data"]["status"] == "cancelled"

    def test_retry_after_remove(self, _tx_env):
        """retry re-adds the torrent via RPC after it was removed."""
        if len(_tx_env["tasks"]) < 2:
            pytest.skip("need a second task")
        task = _tx_env["tasks"][1]
        tid = task["id"]

        # Remove from the daemon, then retry → fresh add_torrent
        r = _api(f"/api/v1/tasks/{tid}", method="delete")
        assert r.status_code == 200
        r = _api(f"/api/v1/tasks/{tid}/retry", method="post")
        assert r.status_code == 200, f"retry failed: {r.text}"
        data = r.json()["data"]
        assert data["status"] == "downloading", f"retry should re-add: {data}"
