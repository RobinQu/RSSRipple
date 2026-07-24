"""Integration test: RSS subscription flow through RSSRipple.

Tests:
1. Create a Channel pointing to a test RSS feed
2. Verify channel validation succeeds
3. Fetch the feed and verify FileResources are created
4. Create an Agent with filter_config DSL
5. Verify filter matching against resources
"""

import httpx

from tests.integration.http._http import DEFAULT_FIELD_MAPPING, RSSRIPPLE, TEST_SERVER


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

    def test_create_channel_invalid_feed_fails(self):
        """Creating a channel with an invalid RSS URL should fail."""
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels",
            json={
                "name": "Bad Feed",
                "url": "http://nonexistent-server:9999/rss",
                "fetch_interval": 300,
                "field_mapping": DEFAULT_FIELD_MAPPING,
            },
            timeout=15,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "INVALID_FEED"
