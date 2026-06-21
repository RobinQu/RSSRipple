"""Integration test: Torrent lifecycle.

Tests:
1. Create a torrent from a test file
2. Seed it via the test server
3. Download it via a second client
4. Assert download is complete and file matches
"""

import time
import httpx
import pytest

TEST_SERVER = "http://test-server:8080"


class TestTorrentLifecycle:
    """End-to-end torrent create → seed → download → verify."""

    def test_create_torrent(self):
        """Create a .torrent from a known test file."""
        resp = httpx.post(
            f"{TEST_SERVER}/api/torrents/create",
            params={"file_name": "[LoliHouse] 黄泉使者 - 01 [1080p HEVC-10bit AAC][简繁内封字幕].mkv"},
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"]
        assert "info_hash" in data["data"]
        assert data["data"]["torrent_url"].endswith(".torrent")

    def test_serve_torrent(self):
        """Verify .torrent file is downloadable."""
        resp = httpx.post(
            f"{TEST_SERVER}/api/torrents/create",
            params={"file_name": "[ANi] 黄泉使者 - 02 [1080p AVC AAC][CHT].mkv"},
            timeout=10,
        )
        assert resp.json()["success"]
        info_hash = resp.json()["data"]["info_hash"]

        resp = httpx.get(f"{TEST_SERVER}/torrents/{info_hash}.torrent", timeout=10)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-bittorrent"
        assert len(resp.content) > 0

    def test_seed_and_download(self):
        """Full lifecycle: create → seed → download → verify."""
        # 1. Create torrent
        resp = httpx.post(
            f"{TEST_SERVER}/api/torrents/create",
            params={"file_name": "[ANi] 葬送的芙莉莲 - 01 [1080p AVC AAC][CHT].mkv"},
            timeout=10,
        )
        assert resp.status_code == 200
        info_hash = resp.json()["data"]["info_hash"]

        # 2. Seed
        resp = httpx.post(f"{TEST_SERVER}/api/torrents/seed/{info_hash}", timeout=10)
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "seeding"

        # 3. Download (uses a separate libtorrent session)
        resp = httpx.post(f"{TEST_SERVER}/api/torrents/download/{info_hash}", timeout=10)
        assert resp.status_code == 200

        # 4. Wait for download to complete
        for _ in range(20):
            time.sleep(1)
            resp = httpx.get(f"{TEST_SERVER}/api/torrents/{info_hash}/status", timeout=10)
            status = resp.json()["data"]["status"]
            if status == "complete":
                break

        # 5. Assert complete
        resp = httpx.post(f"{TEST_SERVER}/api/torrents/{info_hash}/assert-complete", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"], f"Download not verified: {data['data']['message']}"

    def test_tracker_announce(self):
        """Verify the tracker responds to announce requests."""
        import hashlib
        info_hash = hashlib.sha1(b"test-announce").digest()

        resp = httpx.get(
            f"{TEST_SERVER}/announce",
            params={
                "info_hash": info_hash.decode("latin-1"),
                "peer_id": "-LT1234-123456789012",
                "port": 6881,
                "uploaded": 0,
                "downloaded": 0,
                "left": 1000,
                "event": "started",
            },
            timeout=10,
        )
        assert resp.status_code == 200
        # Response should be bencoded
        assert len(resp.content) > 0

    def test_tracker_scrape(self):
        """Verify the tracker responds to scrape requests."""
        resp = httpx.get(f"{TEST_SERVER}/scrape", timeout=10)
        assert resp.status_code == 200
        assert len(resp.content) > 0
