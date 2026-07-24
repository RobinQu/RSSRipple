"""Shared HTTP helpers for integration tests against the dockerized RSSRipple app.

Consolidates the ``_client`` / ``_api`` / ``_poll_fetch`` / ``_poll_run`` /
``_ensure_downloader`` helpers and ``DEFAULT_FIELD_MAPPING`` that were
previously copy-pasted across test_e2e_pipeline, test_agent_pipeline,
test_channel_real_feeds, test_metadata_pipeline, and friends.
"""

from __future__ import annotations

import os
import time

import httpx

# ── Environment ──────────────────────────────────────────────────────────

RSSRIPPLE = os.environ.get("RSSRIPPLE_URL", "http://app:9001")
TEST_SERVER = os.environ.get("TEST_SERVER_URL", "http://test-server:8080")
MIKANANI_EXT_URL = f"{TEST_SERVER}/rss/mikanani-ext"
MIKANANI_1_URL = f"{TEST_SERVER}/rss/mikanani-1"
TIMEOUT = 60.0


def _client() -> httpx.Client:
    """Fresh HTTP client against the RSSRipple app."""
    return httpx.Client(timeout=TIMEOUT)


def _api(path: str, method: str = "get", **kw):
    """Convenience HTTP call against the RSSRipple app (with 3x retry)."""
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


def _poll_fetch(channel_id: str, timeout: int = 120, accept_failed: bool = False) -> dict:
    """Block until the channel fetch job reaches a terminal state.

    Returns the inner ``data`` dict. By default only ``done`` is terminal
    (matches the channel ground-truth tests). Pass ``accept_failed=True`` to
    also treat ``failed`` as terminal - used by the e2e/agent pipeline tests
    that tolerate a failed fetch rather than waiting out the full timeout.
    """
    terminal = ("done", "failed") if accept_failed else ("done",)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api(f"/api/v1/channels/{channel_id}/fetch-status")
        data = r.json().get("data") or {}
        if data.get("status") in terminal:
            return data
        time.sleep(2)
    raise TimeoutError(f"Fetch did not complete for channel {channel_id}")


def _poll_run(agent_id: str, timeout: int = 120) -> dict:
    """Block until the agent run job finishes (done/failed) or times out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api(f"/api/v1/agents/{agent_id}/run-status")
        data = r.json().get("data") or {}
        if data.get("status") in ("done", "failed"):
            return data
        time.sleep(2)
    raise TimeoutError(f"Agent run did not complete for agent {agent_id}")


def _get_first_downloader_id() -> str | None:
    """Get the ID of the first downloader, or None if none exist."""
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    if r.status_code != 200:
        return None
    data = r.json().get("data", [])
    if not data:
        return None
    return data[0]["id"]


def _ensure_downloader() -> str:
    """Get or create a Transmission downloader. Returns the downloader ID."""
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
