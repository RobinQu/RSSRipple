"""Metadata source coverage tests against the mock-LLM app instance.

Drives the UnifiedMetadataAgent ReAct loop with each single-source tool
surface (tmdb / jina / exa) on app-llm. Fake API keys are configured so the
per-source search helpers run their request-building code and fail fast on
the (unreachable/401) external call — deterministic in both directions (the
tool returns an error payload either way, and the mock LLM then finalizes
found=false).

Also re-fetches the channel once: the second fetch runs the metadata
backfill phase for retry-eligible unmatched resources (fetch_service).

Skipped entirely when RSSRIPPLE_LLM_URL is not set.
"""

from __future__ import annotations

import os

import pytest

from tests.integration.http.test_llm_mock import (
    MIKANANI_S2_URL,
    RICH_FIELD_MAPPING,
    _api,
    _poll_fetch,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("RSSRIPPLE_LLM_URL"),
    reason="RSSRIPPLE_LLM_URL not set (mock-LLM app not in stack)",
)


@pytest.fixture(scope="class")
def _fake_source_keys():
    """Configure dummy external-source keys on app-llm (restored after)."""
    r = _api(
        "/api/v1/system-settings",
        method="put",
        json={"tmdb_api_key": "mock-tmdb", "jina_api_key": "mock-jina",
              "exa_api_key": "mock-exa"},
    )
    assert r.status_code == 200, f"set fake keys failed: {r.text}"
    yield
    _api(
        "/api/v1/system-settings",
        method="put",
        json={"tmdb_api_key": "", "jina_api_key": "", "exa_api_key": ""},
    )


class TestMetadataSourceMatrix:
    """One metadata-agent channel per external source (unknown titles)."""

    @pytest.mark.parametrize("source", ["tmdb", "jina", "exa"])
    def test_source_channel_not_found(self, _fake_source_keys, source: str):
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": f"LLM Source Channel ({source})",
                "url": MIKANANI_S2_URL,  # 药屋少女的呢喃 — not a canned work
                "field_mapping": RICH_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": True,
                "metadata_source": source,
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.text}"
        ch_id = r.json()["data"]["id"]

        try:
            _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
            result = _poll_fetch(ch_id)
            assert result.get("status") == "done", f"fetch failed: {result}"

            # Second fetch: no new entries, but the backfill phase re-runs
            # retry-eligible unmatched resources (transient errors from the
            # dead external endpoints are immediately retry-eligible... or
            # skipped by backoff — either way the phase executes).
            _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
            result = _poll_fetch(ch_id)
            assert result.get("status") == "done", f"re-fetch failed: {result}"

            r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
            resources = r.json().get("data", [])
            assert resources
            # Mock finalizes found=false for non-canned titles → unlinked
            unlinked = [
                res for res in resources
                if not res.get("series_id") and not res.get("movie_id")
            ]
            assert len(unlinked) == len(resources)
        finally:
            _api(f"/api/v1/channels/{ch_id}", method="delete")
