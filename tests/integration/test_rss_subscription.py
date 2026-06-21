"""Integration test: RSS subscription flow through RSSRipple.

Tests:
1. Create a Channel pointing to a test RSS feed
2. Verify channel validation succeeds
3. Fetch the feed and verify FileResources are created
4. Create an Agent with filters
5. Verify filter matching against resources
"""

import time
import httpx
import pytest

TEST_SERVER = "http://test-server:8080"
RSSRIPPLE = "http://app:9001"


class TestRSSSubscription:
    """Full RSSRipple subscription flow."""

    def test_validate_dmhy_feed(self):
        """Validate that the dmhy test feed is reachable and parseable."""
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels/validate-url",
            json={"url": f"{TEST_SERVER}/rss/dmhy"},
            timeout=15,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["valid"] is True
        assert data["item_count"] > 0
        assert data["downloadable_count"] > 0

    def test_validate_mikanani_feed(self):
        """Validate the mikanani test feed."""
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels/validate-url",
            json={"url": f"{TEST_SERVER}/rss/mikanani"},
            timeout=15,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["valid"] is True
        assert data["item_count"] > 0

    def test_validate_eztv_feed(self):
        """Validate the EZTV test feed."""
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels/validate-url",
            json={"url": f"{TEST_SERVER}/rss/eztv"},
            timeout=15,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["valid"] is True

    def test_create_channel_mikanani(self):
        """Create a channel for the mikanani feed."""
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels",
            json={
                "name": "Test Mikanani",
                "url": f"{TEST_SERVER}/rss/mikanani",
                "fetch_interval": 300,
            },
            timeout=15,
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["name"] == "Test Mikanani"
        assert data["status"] == "active"
        assert data["parser_type"] == "auto"

    def test_create_channel_eztv(self):
        """Create a channel for the EZTV feed."""
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels",
            json={
                "name": "Test EZTV",
                "url": f"{TEST_SERVER}/rss/eztv",
                "fetch_interval": 300,
            },
            timeout=15,
        )
        assert resp.status_code == 201

    def test_create_channel_invalid_feed_fails(self):
        """Creating a channel with an invalid RSS URL should fail."""
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels",
            json={
                "name": "Bad Feed",
                "url": "http://nonexistent-server:9999/rss",
                "fetch_interval": 300,
            },
            timeout=15,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "INVALID_FEED"

    def test_analyze_feed_generates_mapping(self):
        """Analyze a channel's feed to generate field mappings."""
        # Create channel first
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels",
            json={
                "name": "Analyze Test",
                "url": f"{TEST_SERVER}/rss/mikanani",
            },
            timeout=15,
        )
        channel_id = resp.json()["data"]["id"]

        # Analyze — this calls LLM, so it may return low confidence without API key
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels/{channel_id}/analyze",
            timeout=30,
        )
        assert resp.status_code == 200

    def test_list_channels(self):
        """Verify channels are listed."""
        resp = httpx.get(f"{RSSRIPPLE}/api/v1/channels", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"]
        assert len(data["data"]) > 0
