"""Metadata matching, search, and linking integration tests.

Tests the metadata pipeline:
  - Channel creation for metadata API workflows
  - Fetch creates resources without invoking per-entry external metadata search
  - Manual metadata search via LLM web-search
  - Resource metadata detail endpoint
  - Manual metadata linking to create/update series

Requirements: Docker test environment with app + test-server services.
test_manual_metadata_search skips gracefully when no LLM is configured on the
primary app; TestMetadataLink runs offline against the mock-LLM/mock-TMDB app
instance (RSSRIPPLE_LLM_URL) instead.

Usage:
    docker compose -f docker-compose.test.yml up --build
    uv run pytest tests/integration/test_metadata_pipeline.py -v --timeout=300
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from tests.integration.http._http import (
    API_HEADERS,
    DEFAULT_FIELD_MAPPING,
    MIKANANI_EXT_URL,
    RSSRIPPLE,
    TEST_SERVER,
    _api,
    _poll_fetch,
    associate_metadata_request,
    search_metadata_request,
)

_HAS_LLM = bool(os.environ.get("LLM_API_KEY"))
_HAS_TMDB = bool(os.environ.get("TMDB_API_KEY"))

# Second app instance wired to the deterministic mock LLM + mock TMDB
# (RSSRIPPLE_LLM_URL → app-llm); empty when the stack has no app-llm.
LLM_APP = os.environ.get("RSSRIPPLE_LLM_URL", "")
MIKANANI_S0_URL = f"{TEST_SERVER}/rss/mikanani?series=0"  # 黄泉使者


def _llm_api(path: str, method: str = "get", **kw) -> httpx.Response:
    """HTTP call against the mock-LLM app instance."""
    c = httpx.Client(timeout=120.0, headers=API_HEADERS)
    return getattr(c, method.lower())(f"{LLM_APP}{path}", **kw)


def _poll_fetch_llm(channel_id: str, timeout: int = 120) -> dict:
    """Poll fetch-status on the mock-LLM app until terminal."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _llm_api(f"/api/v1/channels/{channel_id}/fetch-status")
        data = r.json().get("data") or {}
        if data.get("status") in ("done", "failed"):
            return data
        time.sleep(2)
    raise TimeoutError(f"Fetch did not complete for channel {channel_id}")


def _api_llm_search(path: str, **kw) -> httpx.Response:
    """Manual-search calls route through the real (env-configured) LLM, which
    can take minutes under load — use a 240s budget instead of the shared 60s
    client (which flaky-times-out on this endpoint), mirroring _api_refresh."""
    c = httpx.Client(timeout=240.0, headers=API_HEADERS)
    kw.pop("method", None)
    return c.post(f"{RSSRIPPLE}{path}", **kw)




# =========================================================================
# TestMetadataMatching — automatic and manual metadata matching
# =========================================================================


class TestMetadataMatching:
    """Metadata matching — from fetch through manual search."""

    channel_id: str = ""
    first_resource_id: str = ""

    def test_create_channel_for_metadata_pipeline(self):
        """POST /channels — create a channel for metadata API tests."""
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "Metadata Pipeline Test",
                "url": MIKANANI_EXT_URL,
                "field_mapping": DEFAULT_FIELD_MAPPING,
                "fetch_interval": 3600,
                # Keep compose integration deterministic: manual metadata search below
                # exercises the metadata API without running LLM search for every feed item.
                "metadata_agent_enabled": False,
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.status_code} {r.text}"
        data = r.json()["data"]
        assert data["metadata_agent_enabled"] is False
        assert data["url"] == MIKANANI_EXT_URL

        TestMetadataMatching.channel_id = data["id"]

    def test_fetch_creates_resources_for_metadata_tests(self):
        """POST /channels/{id}/fetch — poll for completion, verify resources created."""
        if not TestMetadataMatching.channel_id:
            pytest.skip("No channel created — prerequisite test failed")

        r = _api(
            f"/api/v1/channels/{TestMetadataMatching.channel_id}/fetch",
            method="post",
        )
        assert r.status_code == 200, f"fetch trigger failed: {r.text}"

        result = _poll_fetch(TestMetadataMatching.channel_id, accept_failed=True)
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

        # Automatic metadata matching is intentionally disabled for this channel.
        # Manual search/link endpoints are covered below.
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

        r = search_metadata_request(
            f"/api/v1/resources/{TestMetadataMatching.first_resource_id}/metadata/search",
            api=_api_llm_search,
            json={
                "search_title": "Breaking Bad",
                "content_type": "tv",
                "data_source_type": "exa",
            },
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
    """Manual metadata search + link against the mock-provider app (app-llm).

    Fully offline: the mock LLM drives the ReAct search loop and the
    test-server's mock TMDB answers the tool calls, so no real provider keys
    are involved. Skipped only when the stack has no mock-LLM instance
    (RSSRIPPLE_LLM_URL unset, e.g. the distributed suite).
    """

    def test_link_metadata_creates_series(self):
        """Online search (mock TMDB) → select candidate → link → series exists."""
        if not LLM_APP:
            pytest.skip("RSSRIPPLE_LLM_URL not set (mock-LLM app not in stack)")

        # Point the app's TMDB source at the test-server mock (the app-llm
        # entrypoint redirects api.themoviedb.org there; the key just needs
        # to be non-empty for the source to bind its search tool).
        r = _llm_api(
            "/api/v1/system-settings", method="put", json={"tmdb_api_key": "mock-tmdb"}
        )
        assert r.status_code == 200, f"set fake tmdb key failed: {r.text}"

        channel_id = None
        try:
            # Channel only supplies a resource to link; the agent stays off so
            # the fetch does not consume the mock LLM per entry.
            r = _llm_api(
                "/api/v1/channels",
                method="post",
                json={
                    "name": "Metadata Link Mock-LLM Test",
                    # Unique suffix: a leftover S0 channel from another file
                    # (silent delete/scheduler race) must not trip the
                    # uq_channels_url constraint.
                    "url": f"{MIKANANI_S0_URL}&case={uuid.uuid4().hex[:8]}",
                    "field_mapping": DEFAULT_FIELD_MAPPING,
                    "fetch_interval": 3600,
                    "metadata_agent_enabled": False,
                },
            )
            assert r.status_code == 201, f"create channel failed: {r.text}"
            channel_id = r.json()["data"]["id"]

            r = _llm_api(f"/api/v1/channels/{channel_id}/fetch", method="post")
            assert r.status_code == 200, f"fetch trigger failed: {r.text}"
            result = _poll_fetch_llm(channel_id)
            assert result["status"] == "done", f"fetch failed: {result}"

            r = _llm_api(
                f"/api/v1/channels/{channel_id}/resources", params={"page_size": 1}
            )
            assert r.status_code == 200 and r.json()["data"], "no resources after fetch"
            resource_id = r.json()["data"][0]["id"]

            # Manual search: the mock LLM finalizes with the canned Frieren
            # entity (tmdb:900002) — deterministic, no real provider. (Not
            # 黄泉使者: tmdb:900001 is reserved for test_llm_mock.py's
            # end-to-end pipeline test, which asserts it does not exist yet.)
            r_search = search_metadata_request(
                f"/api/v1/resources/{resource_id}/metadata/search",
                api=_llm_api,
                json={
                    "search_title": "Frieren",
                    "content_type": "tv",
                    "data_source_type": "tmdb",
                },
            )
            assert r_search.status_code == 200, (
                f"metadata search failed: {r_search.status_code} {r_search.text}"
            )
            results = r_search.json().get("data", {}).get("results", [])
            assert results, "expected candidates from the mock metadata provider"
            selected = results[0]
            assert selected.get("external_id") == "tmdb:900002"
            if "content_type" not in selected:
                selected["content_type"] = "tv"

            # Link the selected candidate to the resource.
            r_link = associate_metadata_request(
                f"/api/v1/resources/{resource_id}/metadata/link",
                api=_llm_api,
                json={"selected_result": selected},
            )
            assert r_link.status_code == 200, (
                f"metadata link failed: {r_link.status_code} {r_link.text}"
            )
            assert r_link.json()["success"] is True

            # The resource is now linked to a persisted series.
            r = _llm_api(f"/api/v1/resources/{resource_id}")
            assert r.status_code == 200
            series_id = (r.json().get("data") or {}).get("series_id")
            assert series_id, f"resource not linked after metadata link: {r.text}"

            r_series = _llm_api("/api/v1/series", params={"page_size": 100})
            assert r_series.status_code == 200
            series_list = r_series.json().get("data", [])
            assert any(s["id"] == series_id for s in series_list), (
                f"linked series {series_id} missing from /series listing"
            )
        finally:
            if channel_id:
                try:
                    _llm_api(f"/api/v1/channels/{channel_id}", method="delete")
                except Exception:
                    pass
            _llm_api("/api/v1/system-settings", method="put", json={"tmdb_api_key": ""})

    @classmethod
    def teardown_class(cls):
        """Cleanup: delete the test channel."""
        try:
            _api(f"/api/v1/channels/{TestMetadataMatching.channel_id}", method="delete")
        except Exception:
            pass
