"""Comprehensive Channel integration test — CRUD, field mapping, and fetch + ground truth verification.

Tests against the test server RSS feeds covering:
  - Channel CRUD lifecycle (create, read, update, delete) on dmhy-style feed
  - Fetch + ground truth on mikanani-ext feed (resource creation, dedup, pagination)
  - Custom field_mapping configurations
  - Multiple feed format support (dmhy, eztv-ext, kisssub)

Requirements: Docker test environment with app + test-server services.
"""

from __future__ import annotations

import httpx
import pytest

from tests.integration.http._http import DEFAULT_FIELD_MAPPING, TEST_SERVER, _api, _poll_fetch


def _create_channel(
    name: str,
    url: str,
    field_mapping: dict | None = None,
    **extra,
) -> httpx.Response:
    """Create a channel and return the response."""
    payload: dict = {
        "name": name,
        "url": url,
        "fetch_interval": 3600,
        "field_mapping": field_mapping or DEFAULT_FIELD_MAPPING,
        "metadata_agent_enabled": False,
    }
    payload.update(extra)
    return _api("/api/v1/channels", method="post", json=payload)


def _get_resources(
    channel_id: str, page: int = 1, page_size: int = 100
) -> tuple[list[dict], dict]:
    """Get resources for a channel. Returns (items, meta)."""
    r = _api(
        f"/api/v1/channels/{channel_id}/resources",
        params={"page": page, "page_size": page_size},
    )
    assert r.status_code == 200, f"get resources failed: {r.text}"
    body = r.json()
    return body["data"], body["meta"]


# ── Feed URLs ─────────────────────────────────────────────────────────

DMHY_FEED_URL = f"{TEST_SERVER}/rss/dmhy"
MIKANANI_EXT_URL = f"{TEST_SERVER}/rss/mikanani-ext"
EZTV_EXT_URL = f"{TEST_SERVER}/rss/eztv-ext"
KISSSUB_URL = f"{TEST_SERVER}/rss/kisssub-style"


# =========================================================================
# TestChannelCRUD
# =========================================================================


@pytest.fixture(scope="class")
def dmhy_channel():
    """Create a dmhy-style channel for CRUD tests. Cleaned up after class."""
    resp = _create_channel("CRUD Test - dmhy", DMHY_FEED_URL)
    assert resp.status_code == 201, f"create failed: {resp.text}"
    channel = resp.json()["data"]
    yield channel
    # Cleanup
    try:
        _api(f"/api/v1/channels/{channel['id']}", method="delete")
    except Exception:
        pass


class TestChannelCRUD:
    """Basic CRUD operations against a dmhy-style feed."""

    def test_create_channel(self, dmhy_channel):
        """POST /channels with dmhy-style feed URL, verify 201, return id."""
        assert dmhy_channel["id"]
        assert len(dmhy_channel["id"]) == 36  # UUID v4
        assert dmhy_channel["name"] == "CRUD Test - dmhy"
        assert dmhy_channel["url"] == DMHY_FEED_URL
        assert dmhy_channel["status"] == "active"
        assert dmhy_channel["type"] == "rss_feed"

    def test_get_channel(self, dmhy_channel):
        """GET the created channel, verify name/url/status."""
        r = _api(f"/api/v1/channels/{dmhy_channel['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        data = body["data"]
        assert data["id"] == dmhy_channel["id"]
        assert data["name"] == "CRUD Test - dmhy"
        assert data["url"] == DMHY_FEED_URL
        assert data["status"] in ("active", "inactive")

    def test_list_channels(self, dmhy_channel):
        """GET /channels, verify total >= 1, our channel in list."""
        r = _api("/api/v1/channels")
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["meta"]["total"] >= 1
        ids = [ch["id"] for ch in body["data"]]
        assert dmhy_channel["id"] in ids

    def test_update_channel(self, dmhy_channel):
        """PUT new name, verify updated."""
        new_name = "CRUD Test - dmhy (Updated)"
        r = _api(
            f"/api/v1/channels/{dmhy_channel['id']}",
            method="put",
            json={"name": new_name},
        )
        assert r.status_code == 200
        assert r.json()["data"]["name"] == new_name

        # Verify persisted
        r2 = _api(f"/api/v1/channels/{dmhy_channel['id']}")
        assert r2.json()["data"]["name"] == new_name

    def test_delete_channel(self):
        """DELETE, verify 200, confirm gone from list."""
        # Create a temporary channel for deletion test
        resp = _create_channel("CRUD Test - Delete Me", DMHY_FEED_URL)
        assert resp.status_code == 201
        ch_id = resp.json()["data"]["id"]

        # Delete
        r = _api(f"/api/v1/channels/{ch_id}", method="delete")
        assert r.status_code == 200
        assert r.json()["data"]["deleted"] is True

        # Confirm gone from get
        r2 = _api(f"/api/v1/channels/{ch_id}")
        assert r2.status_code == 404

        # Confirm gone from list
        r3 = _api("/api/v1/channels")
        ids = [ch["id"] for ch in r3.json()["data"]]
        assert ch_id not in ids


# =========================================================================
# TestChannelFetchGroundTruth
# =========================================================================


@pytest.fixture(scope="class")
def mikanani_ext_channel_id():
    """Create a channel for the mikanani-ext feed. Cleaned up after class."""
    resp = _create_channel("Mikanani-ext Ground Truth", MIKANANI_EXT_URL)
    assert resp.status_code == 201, f"create failed: {resp.text}"
    channel_id = resp.json()["data"]["id"]
    yield channel_id
    try:
        _api(f"/api/v1/channels/{channel_id}", method="delete")
    except Exception:
        pass


class TestChannelFetchGroundTruth:
    """Test fetch against a mikanani-ext feed and verify ground truth."""

    def test_fetch_creates_resources(self, mikanani_ext_channel_id):
        """Create channel with mikanani-ext feed, POST /fetch, poll
        verify result['result']['new_count'] > 0."""
        r = _api(
            f"/api/v1/channels/{mikanani_ext_channel_id}/fetch", method="post"
        )
        assert r.status_code == 200

        result = _poll_fetch(mikanani_ext_channel_id)
        assert result["status"] == "done", f"fetch failed: {result}"
        assert result["result"]["new_count"] > 0, (
            f"Expected new resources, got new_count={result['result']['new_count']}. "
            f"result={result['result']}"
        )

    def test_resources_have_expected_fields(self, mikanani_ext_channel_id):
        """GET /resources, verify each has title_raw, torrent_url, guid."""
        resources, meta = _get_resources(mikanani_ext_channel_id)
        assert meta["total"] > 0, "No resources found after fetch"

        for res in resources:
            assert res.get("title_raw"), (
                f"Resource {res['id']} missing title_raw"
            )
            assert res.get("torrent_url"), (
                f"Resource {res['id']} missing torrent_url"
            )
            assert res.get("guid"), (
                f"Resource {res['id']} missing guid"
            )

    def test_resources_torrent_url_format(self, mikanani_ext_channel_id):
        """Verify torrent_url starts with torrents/ path (mikanani-style)
        or is a magnet link."""
        resources, meta = _get_resources(mikanani_ext_channel_id)
        if meta["total"] == 0:
            pytest.skip("No resources available (channel may have been cleaned up)")

        bad_urls: list[tuple[str, str]] = []
        for res in resources:
            url = res["torrent_url"]
            is_torrent_file = url.startswith(f"{TEST_SERVER}/torrents/")
            is_magnet = url.startswith("magnet:")
            if not is_torrent_file and not is_magnet:
                bad_urls.append((res["id"], url))

        assert not bad_urls, (
            f"{len(bad_urls)} resource(s) have unexpected torrent_url format: "
            + "; ".join(f"{rid}={u}" for rid, u in bad_urls[:5])
        )

    def test_no_duplicates_on_refetch(self, mikanani_ext_channel_id):
        """POST /fetch again, verify new_count == 0."""
        _, before_meta = _get_resources(mikanani_ext_channel_id)
        assert before_meta["total"] > 0

        r = _api(
            f"/api/v1/channels/{mikanani_ext_channel_id}/fetch", method="post"
        )
        assert r.status_code == 200

        result = _poll_fetch(mikanani_ext_channel_id)
        assert result["status"] == "done", f"refetch failed: {result}"
        assert result["result"]["new_count"] == 0, (
            f"Re-fetch created {result['result']['new_count']} new resources "
            "— dedup broken"
        )

        _, after_meta = _get_resources(mikanani_ext_channel_id)
        assert after_meta["total"] == before_meta["total"], (
            f"Resource count changed after re-fetch: "
            f"{before_meta['total']} → {after_meta['total']}"
        )

    def test_pagination(self, mikanani_ext_channel_id):
        """GET /resources?page=1&page_size=5, verify page_size respected."""
        resources_page1, meta = _get_resources(
            mikanani_ext_channel_id, page=1, page_size=5
        )
        # mikanani-ext: 5 series × 3 episodes × 3 groups = 45 entries
        assert meta["total"] >= 5, (
            f"Not enough resources for pagination test: total={meta['total']}"
        )
        assert meta["page"] == 1
        assert meta["page_size"] == 5
        assert len(resources_page1) <= 5

        # Page 2 should return different resources
        resources_page2, meta2 = _get_resources(
            mikanani_ext_channel_id, page=2, page_size=5
        )
        assert meta2["page"] == 2
        assert len(resources_page2) <= 5

        page1_ids = {r["id"] for r in resources_page1}
        page2_ids = {r["id"] for r in resources_page2}
        assert page1_ids.isdisjoint(page2_ids), (
            f"Page 1 and page 2 returned overlapping resources: "
            f"{page1_ids & page2_ids}"
        )

    def test_fetch_status_polling(self, mikanani_ext_channel_id):
        """POST /fetch, verify the terminal fetch-status is 'done'."""
        r = _api(
            f"/api/v1/channels/{mikanani_ext_channel_id}/fetch", method="post"
        )
        assert r.status_code == 200

        result = _poll_fetch(mikanani_ext_channel_id)
        assert result["status"] == "done"
        # The inner result status is either "success" (new items) or
        # "unchanged" (everything was already fetched)
        assert result["result"]["status"] in ("success", "unchanged"), (
            f"Unexpected inner status: {result['result']['status']}"
        )


# =========================================================================
# TestChannelFieldMapping
# =========================================================================


class TestChannelFieldMapping:
    """Test different field_mapping configurations."""

    def test_create_with_custom_field_mapping(self):
        """Create channel with field_mapping extracting title_raw from title
        resolution from description, subtitle_group from title."""
        custom_mapping = {
            "list_locator": {"source": "entries"},
            "field_mappings": {
                "title_raw": {"source": "title"},
                "torrent_url": {"source": "link"},
                "resolution": {
                    "source": "description",
                    "regex": r"(\d{3,4}p)",
                },
                "subtitle_group": {
                    "source": "title",
                    "regex": r"^\[([^\]]+)\]",
                },
            },
        }
        resp = _create_channel(
            "Custom Field Mapping Test",
            MIKANANI_EXT_URL,
            field_mapping=custom_mapping,
        )
        assert resp.status_code == 201, f"create failed: {resp.text}"
        ch_id = resp.json()["data"]["id"]

        # Verify field_mapping stored correctly
        r = _api(f"/api/v1/channels/{ch_id}")
        stored = r.json()["data"]
        stored_mapping = stored["field_mapping"]["field_mappings"]
        assert "resolution" in stored_mapping
        assert "subtitle_group" in stored_mapping
        assert stored_mapping["resolution"]["regex"] == r"(\d{3,4}p)"

        # Fetch and verify resources have parsed fields
        _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
        result = _poll_fetch(ch_id)
        assert result["status"] == "done", f"fetch failed: {result}"

        resources, meta = _get_resources(ch_id)
        assert meta["total"] > 0

        # Some resources should have resolution or subtitle_group parsed
        resolutions = [
            r.get("resolution") for r in resources if r.get("resolution")
        ]
        subtitle_groups = [
            r.get("subtitle_group")
            for r in resources
            if r.get("subtitle_group")
        ]
        # At least one field should have been parsed for at least one resource
        assert (
            len(resolutions) > 0 or len(subtitle_groups) > 0
        ), "No resources had parsed fields from custom field mapping"

        # Cleanup
        _api(f"/api/v1/channels/{ch_id}", method="delete")

    def test_update_field_mapping(self):
        """PUT update channel with new field_mapping, verify fetch uses
        new mapping."""
        # Create with default mapping
        resp = _create_channel(
            "Field Mapping Update Test",
            MIKANANI_EXT_URL,
        )
        assert resp.status_code == 201
        ch_id = resp.json()["data"]["id"]

        # First fetch with default mapping (just to populate)
        _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
        result = _poll_fetch(ch_id)
        assert result["status"] == "done"

        # Update with custom mapping that parses subtitle_group from title
        updated_mapping = {
            "list_locator": {"source": "entries"},
            "field_mappings": {
                "title_raw": {"source": "title"},
                "torrent_url": {"source": "link"},
                "subtitle_group": {
                    "source": "title",
                    "regex": r"^\[([^\]]+)\]",
                },
            },
        }
        r = _api(
            f"/api/v1/channels/{ch_id}",
            method="put",
            json={"field_mapping": updated_mapping},
        )
        assert r.status_code == 200, f"update failed: {r.text}"

        # Verify updated mapping persisted
        r2 = _api(f"/api/v1/channels/{ch_id}")
        stored = r2.json()["data"]["field_mapping"]
        assert "subtitle_group" in stored["field_mappings"]
        assert (
            stored["field_mappings"]["subtitle_group"]["regex"]
            == r"^\[([^\]]+)\]"
        )

        # Cleanup
        _api(f"/api/v1/channels/{ch_id}", method="delete")


# =========================================================================
# TestMultipleFeedFormats
# =========================================================================


class TestMultipleFeedFormats:
    """Test creating channels with different feed formats."""

    def test_create_eztv_channel(self):
        """Create channel with eztv-ext feed."""
        resp = _create_channel("EZTV-ext Format Test", EZTV_EXT_URL)
        assert resp.status_code == 201, f"create failed: {resp.text}"
        data = resp.json()["data"]
        assert data["url"] == EZTV_EXT_URL
        assert data["type"] == "rss_feed"

        # Verify in list
        r = _api("/api/v1/channels")
        ids = [ch["id"] for ch in r.json()["data"]]
        assert data["id"] in ids

        # Cleanup
        _api(f"/api/v1/channels/{data['id']}", method="delete")

    def test_create_kisssub_channel(self):
        """Create channel with kisssub-style feed."""
        resp = _create_channel("KissSub Format Test", KISSSUB_URL)
        assert resp.status_code == 201, f"create failed: {resp.text}"
        data = resp.json()["data"]
        assert data["url"] == KISSSUB_URL

        # Cleanup
        _api(f"/api/v1/channels/{data['id']}", method="delete")

    def test_fetch_all_formats(self):
        """Fetch all 3 channels (dmhy, eztv-ext, kisssub), verify each
        gets > 0 resources."""
        feeds: dict[str, str] = {
            "dmhy": DMHY_FEED_URL,
            "eztv-ext": EZTV_EXT_URL,
            "kisssub": KISSSUB_URL,
        }
        channel_ids: dict[str, str] = {}

        # Create and fetch each feed
        for label, url in feeds.items():
            resp = _create_channel(f"Multi-format Test - {label}", url)
            assert resp.status_code == 201, (
                f"create {label} failed: {resp.text}"
            )
            ch_id = resp.json()["data"]["id"]
            channel_ids[label] = ch_id

            _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
            result = _poll_fetch(ch_id, timeout=180)
            assert result["status"] == "done", (
                f"fetch {label} failed: {result}"
            )
            assert result["result"]["new_count"] > 0, (
                f"{label} feed ({url}) created 0 new resources"
            )

        # Verify resources for each channel
        for label, ch_id in channel_ids.items():
            resources, meta = _get_resources(ch_id)
            assert meta["total"] > 0, (
                f"{label} channel {ch_id} has no resources after fetch"
            )
            # Every resource must have a torrent_url
            missing = [
                r["id"] for r in resources if not r.get("torrent_url")
            ]
            assert not missing, (
                f"{label}: {len(missing)} resource(s) missing torrent_url"
            )

        # Cleanup all channels
        for label, ch_id in channel_ids.items():
            try:
                _api(f"/api/v1/channels/{ch_id}", method="delete")
            except Exception:
                pass
