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

The LLM-dependent tests run against the mock-LLM app instance
(``RSSRIPPLE_LLM_URL`` → app-llm, wired to the test-server's deterministic
``/v1/chat/completions`` mock), so they are offline and mandatory in the
standard gate stack. They skip only when the stack has no mock-LLM instance
at all (e.g. the distributed suite). The basic feed smoke tests always run.
"""

import json
import os
import time
import uuid

import httpx
import pytest

TEST_SERVER = os.environ.get("TEST_SERVER_URL", "http://test-server:8080")
RSSRIPPLE = os.environ.get("RSSRIPPLE_URL", "http://app:9001")
# Second app instance wired to the deterministic mock LLM (see
# tests/integration/server/mock_llm.py); empty when the stack has no app-llm.
LLM_APP = os.environ.get("RSSRIPPLE_LLM_URL", "")
MIKANANI_1_URL = f"{TEST_SERVER}/rss/mikanani-1"

_HAS_MOCK_LLM = bool(LLM_APP)

DEFAULT_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_raw": {"source": "title"},
        "torrent_url": {"source": "link"},
    },
}


def _unique_url(url: str) -> str:
    """Per-run unique channel URL — same feed content, no uq_channels_url clash.

    The test-server feed routes ignore unknown query params, and the suffix
    makes channel creation immune to a previous test's channel deletion
    racing the app-llm scheduler (a blocked delete leaves the URL occupied).
    """
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}case={uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _poll_fetch(channel_id: str, base: str = RSSRIPPLE, timeout: int = 120) -> dict:
    """Block until the channel fetch job finishes (done/failed) or times out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = httpx.get(
            f"{base}/api/v1/channels/{channel_id}/fetch-status",
            timeout=10,
        )
        data = resp.json().get("data") or {}
        if data.get("status") in ("done", "failed"):
            return data
        time.sleep(2)
    raise TimeoutError(f"fetch job for channel {channel_id} did not finish within {timeout}s")


def _list_resources(channel_id: str, base: str = RSSRIPPLE) -> tuple[list[dict], int]:
    resp = httpx.get(
        f"{base}/api/v1/channels/{channel_id}/resources",
        params={"page_size": 100},
        timeout=15,
    )
    assert resp.status_code == 200
    body = resp.json()
    return body["data"], body["meta"]["total"]


def _stream_analyze(url: str, base: str) -> tuple[dict | None, str | None]:
    """Call analyze-url-stream and return (field_mapping, confidence).

    Returns (None, None) if the endpoint streams an error event. Against the
    mock-LLM app this is deterministic and fast; a real LLM gateway can take
    minutes (each SDK attempt has a 120s budget and _stream_openai retries up
    to 3x), so keep a generous budget for opt-in live runs.
    """
    field_mapping = None
    confidence = None
    with httpx.stream(
        "POST",
        f"{base}/api/v1/channels/analyze-url-stream",
        json={"url": url},
        timeout=480,
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


def _stream_analyze_channel(channel_id: str, base: str) -> dict | None:
    """Call the channel-ID-based analyze-stream (edit mode) and return field_mapping."""
    field_mapping = None
    with httpx.stream(
        "POST",
        f"{base}/api/v1/channels/{channel_id}/analyze-stream",
        timeout=480,
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
# Analyze + Edit Channel workflow (mock-LLM app, RSSRIPPLE_LLM_URL)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def analyzed_mapping():
    """Run analyze-url-stream ONCE and share the result across all LLM tests.

    Calls the 'create-mode' endpoint POST /channels/analyze-url-stream on the
    mock-LLM app. Skips the whole module only when the stack has no mock-LLM
    instance; a missing mapping from the deterministic mock is a hard failure.
    """
    if not _HAS_MOCK_LLM:
        pytest.skip("RSSRIPPLE_LLM_URL not set (mock-LLM app not in stack)")

    field_mapping, confidence = _stream_analyze(MIKANANI_1_URL, LLM_APP)
    assert field_mapping, (
        "analyze-url-stream on the mock-LLM app returned no field_mapping — "
        "the deterministic mock LLM should always answer"
    )
    return {"field_mapping": field_mapping, "confidence": confidence}


@pytest.fixture(scope="module")
def channel_with_mapping(analyzed_mapping):
    """Create a channel, apply the LLM mapping, fetch resources.

    Shares fixture result across all tests in this module.
    """
    # Create channel — disable LLM title extraction so the 87-entry fetch
    # finishes quickly (title extraction is tested in http/test_fetch_with_real_feed.py).
    # metadata_agent_enabled=False avoids per-entry LLM metadata search hangs.
    resp = httpx.post(
        f"{LLM_APP}/api/v1/channels",
        json={
            "name": "Mikanani-1 LLM Test",
            "url": _unique_url(MIKANANI_1_URL),
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
        f"{LLM_APP}/api/v1/channels/{channel_id}",
        json={"field_mapping": analyzed_mapping["field_mapping"]},
        timeout=15,
    )
    assert resp.status_code == 200, f"update channel failed: {resp.text}"

    # Fetch resources with the mapping applied. 87 entries × best-effort
    # external .torrent cache attempts (denied fast by the outbound guard,
    # but slower under parallel-gate load) — budget well above the ~135s
    # observed worst case.
    resp = httpx.post(f"{LLM_APP}/api/v1/channels/{channel_id}/fetch", timeout=30)
    assert resp.status_code == 200
    fetch_result = _poll_fetch(channel_id, base=LLM_APP, timeout=300)

    data = {
        "id": channel_id,
        "field_mapping": analyzed_mapping["field_mapping"],
        "confidence": analyzed_mapping["confidence"],
        "fetch_result": fetch_result,
    }
    yield data
    # Cleanup: delete the channel so tests don't leak state
    try:
        httpx.delete(f"{LLM_APP}/api/v1/channels/{channel_id}", timeout=15)
    except Exception:
        pass


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
        resources, total = _list_resources(channel_with_mapping["id"], base=LLM_APP)
        assert total > 0
        missing = [r["id"] for r in resources if not r.get("torrent_url")]
        assert not missing, f"{len(missing)}/{total} resources have no torrent_url"

    def test_resources_have_title_raw(self, channel_with_mapping):
        resources, total = _list_resources(channel_with_mapping["id"], base=LLM_APP)
        assert total > 0
        blank = [r["id"] for r in resources if not r.get("title_raw")]
        assert not blank, f"{len(blank)}/{total} resources have blank title_raw"

    def test_channel_stream_analyze_also_works(self, channel_with_mapping):
        """Edit-mode endpoint (channel_id-based) also produces a valid mapping."""
        channel_id = channel_with_mapping["id"]
        field_mapping = _stream_analyze_channel(channel_id, LLM_APP)
        assert field_mapping is not None, (
            "channel analyze-stream on the mock-LLM app returned an error event"
        )
        assert "list_locator" in field_mapping
        assert "torrent_url" in field_mapping.get("field_mappings", {})

    def test_no_duplicates_on_refetch(self, channel_with_mapping):
        channel_id = channel_with_mapping["id"]
        _, count_before = _list_resources(channel_id, base=LLM_APP)
        assert count_before > 0

        resp = httpx.post(f"{LLM_APP}/api/v1/channels/{channel_id}/fetch", timeout=30)
        assert resp.status_code == 200
        result = _poll_fetch(channel_id, base=LLM_APP)
        assert result["status"] == "done"
        assert result["result"]["new_count"] == 0, (
            f"Re-fetch created {result['result']['new_count']} new resources — dedup broken"
        )
