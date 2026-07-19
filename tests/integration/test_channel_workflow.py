"""Integration test: full Create Channel → Analyze → Edit → Fetch workflow.

Tests the complete lifecycle using the real mikanani-1.xml fixture served by
the integration test server:

  1. Validate feed URL (test server /rss/mikanani-1)
  2. Create Channel via POST /channels
  3. Analyze feed via POST /channels/analyze-url-stream (create-mode endpoint)
  4. Edit channel — save generated field mappings via PUT /channels/{id}
  5. Fetch resources via POST /channels/{id}/fetch
  6. Verify FileResources are created with correct fields (torrent_url, title_raw)
  7. Re-fetch → deduplication check (no new resources)
  8. Verify channel list includes the new channels

The LLM-dependent tests are skipped automatically if LLM_API_KEY is not set.
The basic CRUD tests (create, fetch, list) always run.
"""

import json
import os
import time

import httpx
import pytest

TEST_SERVER = os.environ.get("TEST_SERVER_URL", "http://test-server:8080")
RSSRIPPLE = os.environ.get("RSSRIPPLE_URL", "http://app:9001")
MIKANANI_1_URL = f"{TEST_SERVER}/rss/mikanani-1"

_HAS_LLM = bool(os.environ.get("LLM_API_KEY"))

DEFAULT_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_raw": {"source": "title"},
        "torrent_url": {"source": "link"},
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _poll_fetch(channel_id: str, timeout: int = 120) -> dict:
    """Block until the channel fetch job finishes (done/failed) or times out."""
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
    raise TimeoutError(f"fetch job for channel {channel_id} did not finish within {timeout}s")


def _list_resources(channel_id: str) -> tuple[list[dict], int]:
    resp = httpx.get(
        f"{RSSRIPPLE}/api/v1/channels/{channel_id}/resources",
        params={"page_size": 100},
        timeout=15,
    )
    assert resp.status_code == 200
    body = resp.json()
    return body["data"], body["meta"]["total"]


def _stream_analyze(url: str) -> tuple[dict | None, str | None]:
    """Call analyze-url-stream and return (field_mapping, confidence).

    Returns (None, None) if LLM returns an error or no mapping.
    """
    field_mapping = None
    confidence = None
    with httpx.stream(
        "POST",
        f"{RSSRIPPLE}/api/v1/channels/analyze-url-stream",
        json={"url": url},
        timeout=180,
    ) as stream:
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if event["type"] == "done":
                field_mapping = event["field_mapping"]
                confidence = event["confidence"]
            elif event["type"] == "error":
                return None, None
    return field_mapping, confidence


def _stream_analyze_channel(channel_id: str) -> dict | None:
    """Call the channel-ID-based analyze-stream (edit mode) and return field_mapping."""
    field_mapping = None
    with httpx.stream(
        "POST",
        f"{RSSRIPPLE}/api/v1/channels/{channel_id}/analyze-stream",
        timeout=180,
    ) as stream:
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if event["type"] == "done":
                field_mapping = event["field_mapping"]
            elif event["type"] == "error":
                return None
    return field_mapping


# ---------------------------------------------------------------------------
# Test server smoke test
# ---------------------------------------------------------------------------

class TestTestServerFeeds:
    """Verify the test server serves all expected RSS feeds."""

    def test_mikanani_1_feed_is_reachable(self):
        resp = httpx.get(f"{TEST_SERVER}/rss/mikanani-1", timeout=10)
        assert resp.status_code == 200
        assert "application/rss" in resp.headers.get("content-type", "")
        assert b"<rss" in resp.content

    def test_mikanani_1_feed_has_items(self):
        resp = httpx.get(f"{TEST_SERVER}/rss/mikanani-1", timeout=10)
        assert resp.content.count(b"<item>") >= 10, "Expected at least 10 feed items"

    def test_mikanani_1_has_torrent_enclosures(self):
        resp = httpx.get(f"{TEST_SERVER}/rss/mikanani-1", timeout=10)
        assert b"application/x-bittorrent" in resp.content

    def test_mikanani_1_has_chinese_titles(self):
        resp = httpx.get(f"{TEST_SERVER}/rss/mikanani-1", timeout=10)
        # Should contain Chinese characters
        content = resp.content.decode("utf-8")
        assert any(ord(c) > 0x4E00 for c in content), "Expected Chinese characters in feed"


# ---------------------------------------------------------------------------
# Create Channel workflow (no LLM — always runs)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def basic_channel_id():
    """Create a channel pointing at the mikanani-1 fixture.

    fetch test finishes quickly without hitting rate limits.
    metadata_agent_enabled=False skips LLM metadata search for each of the 100
    entries, which would otherwise hang when using models that don't support
    the Responses API web_search tool.
    Shared across all tests in TestCreateChannelBasic.
    """
    resp = httpx.post(
        f"{RSSRIPPLE}/api/v1/channels",
        json={
            "name": "Mikanani-1 Basic Test",
            "url": MIKANANI_1_URL,
            "field_mapping": DEFAULT_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
        timeout=15,
    )
    assert resp.status_code == 201, f"create channel failed: {resp.text}"
    return resp.json()["data"]["id"]


class TestCreateChannelBasic:
    """Basic Create Channel workflow: validate → create → fetch → resources."""

    def test_validate_mikanani_1_url(self):
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels/validate-url",
            json={"url": MIKANANI_1_URL},
            timeout=15,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["valid"] is True
        assert data["item_count"] > 0
        assert data["downloadable_count"] > 0

    def test_create_channel_returns_id(self, basic_channel_id):
        assert basic_channel_id, "channel_id must be non-empty"

    def test_channel_appears_in_list(self, basic_channel_id):
        resp = httpx.get(f"{RSSRIPPLE}/api/v1/channels", timeout=10)
        assert resp.status_code == 200
        ids = [ch["id"] for ch in resp.json()["data"]]
        assert basic_channel_id in ids

    def test_channel_fetch_creates_resources(self, basic_channel_id):
        """Trigger fetch and verify resources are created (no field mapping needed)."""
        resp = httpx.post(f"{RSSRIPPLE}/api/v1/channels/{basic_channel_id}/fetch", timeout=30)
        assert resp.status_code == 200
        result = _poll_fetch(basic_channel_id)
        assert result["status"] == "done", f"fetch failed: {result}"
        assert result["result"]["new_count"] > 0, "Expected at least one new resource"

    def test_resources_have_title_raw(self, basic_channel_id):
        resources, total = _list_resources(basic_channel_id)
        assert total > 0
        blank = [r["id"] for r in resources if not r.get("title_raw")]
        assert not blank, f"{len(blank)} resources without title_raw"

    def test_no_duplicates_on_refetch(self, basic_channel_id):
        _, count_before = _list_resources(basic_channel_id)
        assert count_before > 0

        resp = httpx.post(f"{RSSRIPPLE}/api/v1/channels/{basic_channel_id}/fetch", timeout=30)
        assert resp.status_code == 200
        result = _poll_fetch(basic_channel_id)
        assert result["status"] == "done"
        assert result["result"]["new_count"] == 0, (
            f"Re-fetch created {result['result']['new_count']} new resources — dedup broken"
        )

        _, count_after = _list_resources(basic_channel_id)
        assert count_after == count_before


# ---------------------------------------------------------------------------
# Analyze + Edit Channel workflow (requires LLM_API_KEY)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def analyzed_mapping():
    """Run analyze-url-stream ONCE and share the result across all LLM tests.

    Calls the 'create-mode' endpoint POST /channels/analyze-url-stream.
    Skips the entire module if LLM_API_KEY is not set or LLM returns no mapping.
    """
    if not _HAS_LLM:
        pytest.skip("LLM_API_KEY not set — skipping LLM-dependent workflow tests")

    field_mapping, confidence = _stream_analyze(MIKANANI_1_URL)
    if not field_mapping:
        pytest.skip(
            "analyze-url-stream returned no field_mapping (rate limit or LLM error). "
            "Re-run when quota resets."
        )
    return {"field_mapping": field_mapping, "confidence": confidence}


@pytest.fixture(scope="module")
def channel_with_mapping(analyzed_mapping):
    """Create a channel, apply the LLM mapping, fetch resources.

    Shares fixture result across all tests in this module.
    """
    # Create channel — disable LLM title extraction so the 87-entry fetch
    # finishes quickly (title extraction is tested in test_fetch_with_real_feed.py).
    # metadata_agent_enabled=False avoids per-entry LLM metadata search hangs.
    resp = httpx.post(
        f"{RSSRIPPLE}/api/v1/channels",
        json={
            "name": "Mikanani-1 LLM Test",
            "url": MIKANANI_1_URL,
            "field_mapping": DEFAULT_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
        timeout=15,
    )
    assert resp.status_code == 201, f"create channel failed: {resp.text}"
    channel_id = resp.json()["data"]["id"]

    # Edit channel — save the LLM-generated field mapping
    resp = httpx.put(
        f"{RSSRIPPLE}/api/v1/channels/{channel_id}",
        json={"field_mapping": analyzed_mapping["field_mapping"]},
        timeout=15,
    )
    assert resp.status_code == 200, f"update channel failed: {resp.text}"

    # Fetch resources with the mapping applied
    resp = httpx.post(f"{RSSRIPPLE}/api/v1/channels/{channel_id}/fetch", timeout=30)
    assert resp.status_code == 200
    fetch_result = _poll_fetch(channel_id)

    return {
        "id": channel_id,
        "field_mapping": analyzed_mapping["field_mapping"],
        "confidence": analyzed_mapping["confidence"],
        "fetch_result": fetch_result,
    }


class TestAnalyzeUrlStream:
    """Tests for the 'create-mode' analyze-url-stream endpoint."""

    def test_mapping_structure(self, analyzed_mapping):
        fm = analyzed_mapping["field_mapping"]
        assert "list_locator" in fm, "field_mapping missing list_locator"
        assert "field_mappings" in fm, "field_mapping missing field_mappings"

    def test_mapping_has_torrent_url(self, analyzed_mapping):
        mappings = analyzed_mapping["field_mapping"]["field_mappings"]
        assert "torrent_url" in mappings, (
            f"torrent_url missing from LLM mapping. Got: {list(mappings)}"
        )

    def test_mapping_has_title_field(self, analyzed_mapping):
        mappings = analyzed_mapping["field_mapping"]["field_mappings"]
        has_title = "title_cn" in mappings or "title_en" in mappings
        assert has_title, f"Neither title_cn nor title_en in mappings: {list(mappings)}"

    def test_all_rules_have_source_key(self, analyzed_mapping):
        mappings = analyzed_mapping["field_mapping"]["field_mappings"]
        for field_name, rule in mappings.items():
            assert "source" in rule, f"rule for {field_name!r} missing 'source': {rule}"

    def test_confidence_not_low(self, analyzed_mapping):
        assert analyzed_mapping["confidence"] != "low", (
            f"LLM confidence was 'low': {analyzed_mapping}"
        )


class TestEditChannelWithMapping:
    """Tests for applying LLM mapping to a channel and fetching resources."""

    def test_fetch_succeeded(self, channel_with_mapping):
        result = channel_with_mapping["fetch_result"]
        assert result["status"] == "done", (
            f"Fetch ended with status '{result['status']}': {result.get('error')}"
        )

    def test_fetch_created_resources(self, channel_with_mapping):
        result = channel_with_mapping["fetch_result"]
        assert result["result"]["new_count"] > 0, (
            f"Fetch created 0 new resources: {result['result']}"
        )

    def test_resources_have_torrent_url(self, channel_with_mapping):
        resources, total = _list_resources(channel_with_mapping["id"])
        assert total > 0
        missing = [r["id"] for r in resources if not r.get("torrent_url")]
        assert not missing, f"{len(missing)}/{total} resources have no torrent_url"

    def test_resources_have_title_raw(self, channel_with_mapping):
        resources, total = _list_resources(channel_with_mapping["id"])
        assert total > 0
        blank = [r["id"] for r in resources if not r.get("title_raw")]
        assert not blank, f"{len(blank)}/{total} resources have blank title_raw"

    def test_channel_stream_analyze_also_works(self, channel_with_mapping):
        """Edit-mode endpoint (channel_id-based) also produces a valid mapping."""
        channel_id = channel_with_mapping["id"]
        field_mapping = _stream_analyze_channel(channel_id)
        if field_mapping is None:
            pytest.skip("channel analyze-stream returned error (possible rate limit)")
        assert "list_locator" in field_mapping
        assert "torrent_url" in field_mapping.get("field_mappings", {})

    def test_no_duplicates_on_refetch(self, channel_with_mapping):
        channel_id = channel_with_mapping["id"]
        _, count_before = _list_resources(channel_id)
        assert count_before > 0

        resp = httpx.post(f"{RSSRIPPLE}/api/v1/channels/{channel_id}/fetch", timeout=30)
        assert resp.status_code == 200
        result = _poll_fetch(channel_id)
        assert result["status"] == "done"
        assert result["result"]["new_count"] == 0, (
            f"Re-fetch created {result['result']['new_count']} new resources — dedup broken"
        )
