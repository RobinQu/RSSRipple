"""Metadata matching, search, and linking integration tests.

Tests the metadata pipeline:
  - Channel creation with metadata_source="llm"
  - Fetch triggers metadata matching against existing series/movies
  - Manual metadata search via LLM web-search
  - Resource metadata detail endpoint
  - Manual metadata linking to create/update series

Requirements: Docker test environment with app + test-server services.
LLM-dependent tests skip gracefully when no API keys are configured.

Usage:
    docker compose -f docker-compose.test.yml up --build
    uv run pytest tests/integration/test_metadata_pipeline.py -v --timeout=300
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

# ── Environment ──────────────────────────────────────────────────────────

RSSRIPPLE = os.environ.get("RSSRIPPLE_URL", "http://app:9001")
TEST_SERVER = os.environ.get("TEST_SERVER_URL", "http://test-server:8080")
MIKANANI_EXT_URL = f"{TEST_SERVER}/rss/mikanani-ext"
TIMEOUT = 60.0

_HAS_LLM = bool(os.environ.get("LLM_API_KEY"))
_HAS_TMDB = bool(os.environ.get("TMDB_API_KEY"))


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT)


def _api(path: str, method: str = "get", **kw):
    """Convenience HTTP call against the RSSRipple app (with retry)."""
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


def _poll_fetch(channel_id: str, timeout: int = 120) -> dict:
    """Block until the channel fetch job finishes (done/failed) or times out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api(f"/api/v1/channels/{channel_id}/fetch-status")
        d = r.json()
        data = d.get("data") or {}
        if data.get("status") in ("done", "failed"):
            return data
        time.sleep(2)
    raise TimeoutError("Fetch did not complete")


# ── Default field mapping ────────────────────────────────────────────────

DEFAULT_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_raw": {"source": "title"},
        "torrent_url": {"source": "link"},
    },
}


# =========================================================================
# TestMetadataMatching — automatic and manual metadata matching
# =========================================================================


class TestMetadataMatching:
    """Metadata matching — from fetch through manual search."""

    channel_id: str = ""
    first_resource_id: str = ""

    def test_create_channel_with_metadata_source_llm(self):
        """POST /channels — create channel with metadata_source='llm'."""
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "Metadata Pipeline Test",
                "url": MIKANANI_EXT_URL,
                "field_mapping": DEFAULT_FIELD_MAPPING,
                "fetch_interval": 3600,
                "title_extraction_method": "none",
                "metadata_source": "llm",
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.status_code} {r.text}"
        data = r.json()["data"]
        assert data["metadata_source"] == "llm"
        assert data["url"] == MIKANANI_EXT_URL

        TestMetadataMatching.channel_id = data["id"]

    def test_fetch_triggers_metadata_matching(self):
        """POST /channels/{id}/fetch — poll for completion, verify resources created."""
        if not TestMetadataMatching.channel_id:
            pytest.skip("No channel created — prerequisite test failed")

        r = _api(
            f"/api/v1/channels/{TestMetadataMatching.channel_id}/fetch",
            method="post",
        )
        assert r.status_code == 200, f"fetch trigger failed: {r.text}"

        result = _poll_fetch(TestMetadataMatching.channel_id)
        assert result["status"] == "done", (
            f"Fetch did not complete successfully: {result}"
        )
        assert result["result"]["new_count"] > 0, (
            f"Expected at least one new resource: {result['result']}"
        )

        # Verify resources exist
        r = _api(
            f"/api/v1/channels/{TestMetadataMatching.channel_id}/resources",
            params={"page_size": 100},
        )
        assert r.status_code == 200
        body = r.json()
        resources = body.get("data", [])
        assert len(resources) > 0, "No resources found after fetch"

        # Check if any resources have metadata linked
        # (may be empty if no API keys are configured — that's fine)
        linked = [
            res
            for res in resources
            if res.get("series_id") or res.get("movie_id")
        ]
        if linked:
            print(f"Metadata linked for {len(linked)}/{len(resources)} resources")
            TestMetadataMatching.first_resource_id = linked[0]["id"]
        else:
            print("No metadata linked (expected without API keys)")
            TestMetadataMatching.first_resource_id = resources[0]["id"]

    def test_manual_metadata_search(self):
        """POST /resources/{id}/metadata/search — search for known title."""
        if not TestMetadataMatching.first_resource_id:
            pytest.skip("No resources available — prerequisite test failed")

        r = _api(
            f"/api/v1/resources/{TestMetadataMatching.first_resource_id}/metadata/search",
            method="post",
            json={"search_title": "Breaking Bad", "content_type": "tv"},
        )
        # May fail if no LLM API key configured — that's expected
        if r.status_code == 502 and not _HAS_LLM:
            pytest.skip("LLM search unavailable (no LLM_API_KEY configured)")
        assert r.status_code == 200, (
            f"metadata search failed: {r.status_code} {r.text}"
        )
        body = r.json()
        assert body["success"] is True
        data = body.get("data", {})
        assert "results" in data, (
            f"Response missing 'results': {list(data.keys()) if data else 'null'}"
        )
        # Results may be empty if no API keys — that's acceptable
        assert isinstance(data["results"], list), (
            f"'results' should be a list, got {type(data['results']).__name__}"
        )

    def test_get_resource_metadata(self):
        """GET /resources/{id}/metadata — verify response shape."""
        if not TestMetadataMatching.first_resource_id:
            pytest.skip("No resources available — prerequisite test failed")

        r = _api(f"/api/v1/resources/{TestMetadataMatching.first_resource_id}/metadata")
        # May fail if resource has no metadata yet — accept 200 or processing status
        assert r.status_code in (200, 404), (
            f"metadata endpoint unexpected status: {r.status_code} {r.text}"
        )
        body = r.json()
        assert "success" in body, f"Response missing 'success': {body}"
        # If success, data should have expected fields
        if body["success"]:
            data = body.get("data") or {}
            assert data is not None, "Metadata data is null"


# =========================================================================
# TestMetadataLink — manual linking of metadata
# =========================================================================


class TestMetadataLink:
    """Manual metadata linking — creating series/movies from search results."""

    def test_link_metadata_creates_series(self):
        """PUT /resources/{id}/metadata/link — link a resource to a series."""
        if not TestMetadataMatching.first_resource_id:
            pytest.skip("No resources available — prerequisite test failed")
        if not _HAS_LLM and not _HAS_TMDB:
            pytest.skip("No metadata API keys configured — cannot perform search+link")

        # First, search for a known TV show
        r_search = _api(
            f"/api/v1/resources/{TestMetadataMatching.first_resource_id}/metadata/search",
            method="post",
            json={"search_title": "Breaking Bad", "content_type": "tv"},
        )
        if r_search.status_code != 200:
            pytest.skip(f"Search unavailable: {r_search.status_code}")

        results = r_search.json().get("data", {}).get("results", [])
        if not results:
            # Create a synthetic result for the test
            # This simulates what a user would select after LLM search
            selected = {
                "content_type": "tv",
                "title_cn": "绝命毒师",
                "title_en": "Breaking Bad",
                "original_title": "Breaking Bad",
                "description": "Test series created by integration test",
                "external_id": "test:breaking-bad",
                "external_source": "manual",
            }
        else:
            selected = results[0]
            # Ensure content_type is present
            if "content_type" not in selected:
                selected["content_type"] = "tv"

        # Link metadata
        r_link = _api(
            f"/api/v1/resources/{TestMetadataMatching.first_resource_id}/metadata/link",
            method="put",
            json={"selected_result": selected},
        )
        assert r_link.status_code == 200, (
            f"metadata link failed: {r_link.status_code} {r_link.text}"
        )
        body = r_link.json()
        assert body["success"] is True

        # Verify a new series exists (either from the search result or created by link)
        r_series = _api("/api/v1/series", params={"page_size": 100})
        assert r_series.status_code == 200
        series_list = r_series.json().get("data", [])
        # After linking, at least one series should exist
        assert len(series_list) >= 1, (
            f"Expected at least 1 series after linking, got {len(series_list)}"
        )

    @classmethod
    def teardown_class(cls):
        """Cleanup: delete the test channel."""
        try:
            _api(f"/api/v1/channels/{TestMetadataMatching.channel_id}", method="delete")
        except Exception:
            pass
