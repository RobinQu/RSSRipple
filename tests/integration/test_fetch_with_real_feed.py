"""End-to-end integration test: real RSS feed → LLM field mapping → fetch → resources.

Full workflow with a real public RSS feed and live LLM:
  1. Validate the real feed is reachable
  2. Create a channel (no mapping yet)
  3. Call analyze-stream to auto-generate field mappings via LLM
  4. Save the generated mapping back to the channel (LLM title extraction enabled)
  5. Trigger a channel fetch
  6. Verify FileResources are created with correct fields
  7. Re-fetch and verify deduplication (no new resources)

Feed used: Nyaa.si anime English-translated RSS — public, stable, always has torrent enclosures.

Requirements
------------
  - Network access (the app container must be able to reach nyaa.si)
  - LLM_API_KEY env var set (used by the app to call analyze-stream)

Run separately::

    uv run pytest tests/integration/test_fetch_with_real_feed.py -v --timeout=300

Or against a local dev server::

    RSSRIPPLE_URL=http://localhost:9001 \\
    uv run pytest tests/integration/test_fetch_with_real_feed.py -v --timeout=300
"""

import json
import os
import time

import httpx
import pytest

RSSRIPPLE = os.environ.get("RSSRIPPLE_URL", "http://app:9001")

# Targeted Nyaa.si query so the result set is small (≈5-15 entries) and
# all entries share the same show title → LLM title cache fires after the
# first call, keeping the overall runtime manageable.
REAL_FEED_URL = "https://nyaa.si/?page=rss&c=1_2&f=0&q=spy+x+family+1080p"

# Skip is handled inside the fixture (more reliable than module-level pytestmark
# in Docker where load_dotenv may race with env_file injection).


# ---------------------------------------------------------------------------
# Override the integration conftest's autouse fixture: these tests call real
# external services and do not need the local mock RSS / tracker server.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    pass


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

def _poll_fetch(channel_id: str, timeout: int = 240) -> dict:
    """Block until the fetch job reaches done/failed or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = httpx.get(
            f"{RSSRIPPLE}/api/v1/channels/{channel_id}/fetch-status",
            timeout=10,
        )
        data = resp.json().get("data") or {}
        if data.get("status") in ("done", "failed"):
            return data
        time.sleep(3)
    raise TimeoutError(
        f"Fetch job for channel {channel_id} did not finish within {timeout}s"
    )


def _resources(channel_id: str, page_size: int = 100) -> list[dict]:
    resp = httpx.get(
        f"{RSSRIPPLE}/api/v1/channels/{channel_id}/resources",
        params={"page_size": page_size},
        timeout=15,
    )
    assert resp.status_code == 200
    return resp.json()["data"], resp.json()["meta"]["total"]


# ---------------------------------------------------------------------------
# Session-scoped fixture: create the channel once, analyse, fetch, reuse
# ---------------------------------------------------------------------------

@pytest.fixture(scope="class")
def channel(request):
    """Create + analyse + configure + fetch a real Nyaa channel.

    Returns a dict with channel_id, field_mapping, confidence, fetch_result.
    """
    # 1. Validate the feed is reachable from the app
    resp = httpx.post(
        f"{RSSRIPPLE}/api/v1/channels/validate-url",
        json={"url": REAL_FEED_URL},
        timeout=30,
    )
    assert resp.status_code == 200, f"validate-url failed: {resp.text}"
    vdata = resp.json()["data"]
    if not vdata["valid"]:
        pytest.skip(f"Real feed not reachable from app container: {vdata.get('message')}")
    if vdata["downloadable_count"] == 0:
        pytest.skip("Real feed has no downloadable entries — feed may have changed")

    if not os.getenv("LLM_API_KEY"):
        pytest.skip("LLM_API_KEY not set — skipping real-feed integration test")

    # 2. Create channel with LLM title extraction, initially inactive
    # to prevent the scheduler from auto-fetching while we set up mappings.
    resp = httpx.post(
        f"{RSSRIPPLE}/api/v1/channels",
        json={
            "name": "Nyaa Real Feed — e2e test",
            "url": REAL_FEED_URL,
            "field_mapping": DEFAULT_FIELD_MAPPING,
            "fetch_interval": 3600,
            "title_extraction_method": "llm",
            "status": "inactive",
        },
        timeout=30,
    )
    assert resp.status_code == 201, f"create channel failed: {resp.text}"
    channel_id = resp.json()["data"]["id"]

    # 3. Stream LLM analysis to get field mappings
    field_mapping = None
    confidence = None
    with httpx.stream(
        "POST",
        f"{RSSRIPPLE}/api/v1/channels/{channel_id}/analyze-stream",
        timeout=120,
    ) as stream:
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event["type"] == "done":
                field_mapping = event["field_mapping"]
                confidence = event["confidence"]
            elif event["type"] == "error":
                pytest.fail(f"LLM analysis returned error: {event.get('message')}")

    assert field_mapping, "LLM produced no field_mapping"

    # 4. Save the mapping back to the channel and activate it
    resp = httpx.put(
        f"{RSSRIPPLE}/api/v1/channels/{channel_id}",
        json={
            "name": "Nyaa Real Feed — e2e test",
            "field_mapping": field_mapping,
            "title_extraction_method": "llm",
            "status": "active",
        },
        timeout=30,
    )
    assert resp.status_code == 200, f"update channel failed: {resp.text}"

    # 5. Trigger and wait for the first fetch
    resp = httpx.post(
        f"{RSSRIPPLE}/api/v1/channels/{channel_id}/fetch",
        timeout=30,
    )
    assert resp.status_code == 200
    fetch_result = _poll_fetch(channel_id, timeout=600)

    return {
        "id": channel_id,
        "field_mapping": field_mapping,
        "confidence": confidence,
        "fetch_result": fetch_result,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRealFeedEndToEnd:

    def test_llm_generated_mapping_has_torrent_url(self, channel):
        """The LLM-generated mapping must include a torrent_url rule."""
        mappings = channel["field_mapping"]["field_mappings"]
        assert "torrent_url" in mappings, (
            f"torrent_url missing from LLM mapping. Got: {list(mappings)}"
        )

    def test_llm_confidence_not_low(self, channel):
        """LLM analysis should produce at least medium confidence."""
        assert channel["confidence"] in ("medium", "high"), (
            f"Confidence was '{channel['confidence']}' — the LLM may have produced "
            "a partial mapping.  Check LLM_MODEL / LLM_BASE_URL."
        )

    def test_fetch_succeeded(self, channel):
        """The initial fetch job must complete without errors."""
        result = channel["fetch_result"]
        assert result["status"] == "done", (
            f"Fetch ended with status '{result['status']}': {result.get('error')}"
        )

    def test_fetch_created_resources(self, channel):
        """At least one FileResource must exist (scheduler may have auto-fetched)."""
        result = channel["fetch_result"]
        assert result["status"] == "done"
        # new_count can be 0 if the scheduler auto-fetched before the manual fetch.
        # Verify resources actually exist via the resources endpoint.
        resources, total = _resources(channel["id"])
        assert total > 0, (
            f"No resources found for channel {channel['id']}"
        )

    def test_resources_have_torrent_urls(self, channel):
        """Every created FileResource must have a non-empty torrent_url."""
        resources, total = _resources(channel["id"])
        assert total > 0
        missing = [r["id"] for r in resources if not r.get("torrent_url")]
        assert not missing, f"{len(missing)} resource(s) have no torrent_url"

    def test_resources_have_raw_titles(self, channel):
        """Every FileResource must have a non-empty title_raw."""
        resources, total = _resources(channel["id"])
        assert total > 0
        blank = [r["id"] for r in resources if not r.get("title_raw")]
        assert not blank, f"{len(blank)} resource(s) have blank title_raw"

    def test_resources_have_search_titles(self, channel):
        """Resources should have search_title set by LLM title extraction.

        LLM title cache means only the first unique base title triggers a real
        LLM call.  For a targeted query like spy+x+family all entries share the
        same show, so almost all should have search_title after the first call.
        """
        resources, total = _resources(channel["id"])
        assert total > 0
        with_search_title = [r for r in resources if r.get("search_title")]
        ratio = len(with_search_title) / total
        assert ratio >= 0.5, (
            f"Only {len(with_search_title)}/{total} resources have search_title "
            "after LLM extraction — title backfill may have failed"
        )

    def test_search_title_is_clean(self, channel):
        """search_title should not contain typical noise markers."""
        resources, total = _resources(channel["id"])
        noise = ["[", "]", "1080p", "720p", "WEB-DL", "WebRip", " - "]
        dirty = []
        for r in resources:
            st = r.get("search_title") or ""
            if not st:
                continue
            if any(marker in st for marker in noise):
                dirty.append((r["id"], st))
        # Allow up to 20% noisy titles (LLM is non-deterministic)
        if dirty:
            ratio = len(dirty) / total
            assert ratio < 0.2, (
                f"{len(dirty)}/{total} search_titles still contain noise: "
                + ", ".join(f"'{t}'" for _, t in dirty[:3])
            )

    def test_no_duplicate_resources_on_refetch(self, channel):
        """Re-fetching the same feed must not create duplicate FileResources."""
        channel_id = channel["id"]
        _, count_before = _resources(channel_id)
        assert count_before > 0

        # Second fetch
        resp = httpx.post(f"{RSSRIPPLE}/api/v1/channels/{channel_id}/fetch", timeout=30)
        assert resp.status_code == 200
        result = _poll_fetch(channel_id, timeout=180)

        assert result["status"] == "done", f"Re-fetch failed: {result.get('error')}"
        assert result["result"]["new_count"] == 0, (
            f"Re-fetch created {result['result']['new']} new resources — dedup broken"
        )

        _, count_after = _resources(channel_id)
        assert count_after == count_before, (
            f"Resource count changed after re-fetch: {count_before} → {count_after}"
        )
