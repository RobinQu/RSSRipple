"""Integration tests: channel DELETE cascade and form-token double-submit prevention.

These tests run against the live app container (RSSRIPPLE_URL) and the
integration test-server (TEST_SERVER_URL).  They are included in the
docker-compose.test.yml test-runner command automatically.

Covers:
  1. DELETE /channels/{id} with existing file_resources → 200, resources gone
  2. GET /channels/form-token → unique UUID on each call
  3. POST /channels with fresh token → 201 success
  4. POST /channels with same token again → 409 DUPLICATE_SUBMISSION
  5. PUT /channels/{id} with same token twice → 409 on second call
"""

import os
import time

import httpx
import pytest

RSSRIPPLE = os.environ.get("RSSRIPPLE_URL", "http://app:9001")
TEST_SERVER = os.environ.get("TEST_SERVER_URL", "http://test-server:8080")

# Short feed URL that is always reachable in the Docker network
FEED_URL = f"{TEST_SERVER}/rss/mikanani"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_channel(name: str = "Token Test Channel", url: str = FEED_URL,
                    token: str | None = None) -> httpx.Response:
    headers = {"X-Form-Token": token} if token else {}
    return httpx.post(
        f"{RSSRIPPLE}/api/v1/channels",
        json={
            "name": name,
            "url": url,
            "fetch_interval": 3600,
            "field_mapping": {
                "list_locator": {"source": "entries"},
                "field_mappings": {
                    "title_raw": {"source": "title"},
                    "torrent_url": {"source": "link"},
                },
            },
            "metadata_agent_enabled": False,  # avoid per-entry LLM calls that hang in CI
        },
        headers=headers,
        timeout=15,
    )


def _get_form_token() -> str:
    resp = httpx.get(f"{RSSRIPPLE}/api/v1/channels/form-token", timeout=10)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["token"]


def _poll_fetch(channel_id: str, timeout: int = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = httpx.get(
            f"{RSSRIPPLE}/api/v1/channels/{channel_id}/fetch-status",
            timeout=10,
        )
        data = resp.json().get("data") or {}
        if data.get("status") in ("done", "failed"):
            return data
        time.sleep(2)
    raise TimeoutError(f"fetch job for {channel_id} did not finish within {timeout}s")


# ---------------------------------------------------------------------------
# Form token tests
# ---------------------------------------------------------------------------

class TestFormToken:
    """Server-side synchronizer token (CSRF-style double-submit prevention)."""

    def test_form_token_endpoint_returns_uuid(self):
        """GET /channels/form-token returns a 36-char UUID string."""
        token = _get_form_token()
        assert isinstance(token, str)
        assert len(token) == 36
        parts = token.split("-")
        assert len(parts) == 5, f"Not a UUID: {token!r}"

    def test_each_token_is_unique(self):
        """Two consecutive token requests return different tokens."""
        t1 = _get_form_token()
        t2 = _get_form_token()
        assert t1 != t2

    def test_create_channel_with_valid_token_succeeds(self):
        """POST /channels with a fresh form token is accepted (201)."""
        token = _get_form_token()
        resp = _create_channel(name="Token Valid Test", token=token)
        assert resp.status_code == 201, resp.text

    def test_create_channel_duplicate_token_rejected(self):
        """Using the same form token twice returns 409 DUPLICATE_SUBMISSION."""
        token = _get_form_token()

        # First use — consumes the token
        resp1 = _create_channel(name="Dup Test A", token=token)
        assert resp1.status_code == 201, f"First submit failed: {resp1.text}"

        # Second use — token already consumed
        resp2 = _create_channel(name="Dup Test B", token=token)
        assert resp2.status_code == 409, f"Expected 409, got {resp2.status_code}: {resp2.text}"
        body = resp2.json()
        assert body["success"] is False
        assert body["error"]["code"] == "DUPLICATE_SUBMISSION"

    def test_create_channel_without_token_still_works(self):
        """POST /channels without X-Form-Token succeeds (token is optional)."""
        resp = _create_channel(name="No Token Test")
        assert resp.status_code == 201, resp.text

    def test_update_channel_duplicate_token_rejected(self):
        """PUT /channels/{id} with same token twice returns 409 on second call."""
        # Create the channel first (no token needed)
        create_resp = _create_channel(name="Update Token Test")
        assert create_resp.status_code == 201
        channel_id = create_resp.json()["data"]["id"]

        token = _get_form_token()

        # First update — consumes the token
        resp1 = httpx.put(
            f"{RSSRIPPLE}/api/v1/channels/{channel_id}",
            json={"name": "Updated Once"},
            headers={"X-Form-Token": token},
            timeout=15,
        )
        assert resp1.status_code == 200, f"First update failed: {resp1.text}"

        # Second update with the same token — must be rejected
        resp2 = httpx.put(
            f"{RSSRIPPLE}/api/v1/channels/{channel_id}",
            json={"name": "Updated Twice"},
            headers={"X-Form-Token": token},
            timeout=15,
        )
        assert resp2.status_code == 409, f"Expected 409, got {resp2.status_code}: {resp2.text}"
        assert resp2.json()["error"]["code"] == "DUPLICATE_SUBMISSION"


# ---------------------------------------------------------------------------
# DELETE cascade tests
# ---------------------------------------------------------------------------

class TestChannelDeleteCascade:
    """Deleting a channel must cascade-delete its file_resources (and agents)."""

    def test_delete_empty_channel(self):
        """DELETE a channel with no resources succeeds cleanly."""
        resp = _create_channel(name="Delete Empty")
        assert resp.status_code == 201
        channel_id = resp.json()["data"]["id"]

        del_resp = httpx.delete(f"{RSSRIPPLE}/api/v1/channels/{channel_id}", timeout=10)
        assert del_resp.status_code == 200
        assert del_resp.json()["data"]["deleted"] is True

        get_resp = httpx.get(f"{RSSRIPPLE}/api/v1/channels/{channel_id}", timeout=10)
        assert get_resp.status_code == 404

    def test_delete_channel_with_resources_cascades(self):
        """DELETE a channel that has file_resources must not raise 500.

        Regression for: sqlite3.IntegrityError NOT NULL constraint failed:
        file_resources.channel_id — SQLAlchemy was trying to SET channel_id=NULL.
        """
        # Create channel
        resp = _create_channel(name="Delete Cascade Test")
        assert resp.status_code == 201, resp.text
        channel_id = resp.json()["data"]["id"]

        # Fetch to populate file_resources
        fetch_resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels/{channel_id}/fetch", timeout=30
        )
        assert fetch_resp.status_code == 200
        result = _poll_fetch(channel_id)
        assert result["status"] == "done", f"fetch failed: {result}"
        assert result["result"]["new_count"] > 0, "Expected resources to be created"

        # Verify resources exist
        res_resp = httpx.get(
            f"{RSSRIPPLE}/api/v1/channels/{channel_id}/resources",
            params={"page_size": 5},
            timeout=10,
        )
        assert res_resp.status_code == 200
        assert res_resp.json()["meta"]["total"] > 0, "Expected file_resources before delete"

        # Delete the channel — must NOT return 500
        del_resp = httpx.delete(f"{RSSRIPPLE}/api/v1/channels/{channel_id}", timeout=10)
        assert del_resp.status_code == 200, (
            f"DELETE returned {del_resp.status_code} — possible cascade bug: {del_resp.text}"
        )
        assert del_resp.json()["data"]["deleted"] is True

        # Channel must be gone
        get_resp = httpx.get(f"{RSSRIPPLE}/api/v1/channels/{channel_id}", timeout=10)
        assert get_resp.status_code == 404

        # Resources endpoint must 404 (channel is gone) or return empty
        res_after = httpx.get(
            f"{RSSRIPPLE}/api/v1/channels/{channel_id}/resources",
            params={"page_size": 5},
            timeout=10,
        )
        # Either 404 (channel not found) or 200 with total=0 are acceptable
        assert res_after.status_code in (200, 404), res_after.text
        if res_after.status_code == 200:
            assert res_after.json()["meta"]["total"] == 0, "Resources not cleaned up after channel delete"
