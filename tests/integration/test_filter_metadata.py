"""Integration test: Agent filters and metadata matching.

Tests:
1. Create Agent with specific filters
2. Test filters against channel resources
3. Verify metadata source (IMDB) configuration
4. Verify diverse test data exercises filter and metadata paths
"""

import httpx
import pytest

TEST_SERVER = "http://test-server:8080"
RSSRIPPLE = "http://app:9001"


class TestFilterAndMetadata:
    """Agent filter testing and metadata matching."""

    @pytest.fixture
    def mikanani_channel(self):
        """Create a mikanani channel for filter testing."""
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels",
            json={
                "name": "Filter Test - Mikanani",
                "url": f"{TEST_SERVER}/rss/mikanani",
            },
            timeout=15,
        )
        assert resp.status_code == 201
        return resp.json()["data"]["id"]

    @pytest.fixture
    def eztv_channel(self):
        """Create an EZTV channel for filter testing."""
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels",
            json={
                "name": "Filter Test - EZTV",
                "url": f"{TEST_SERVER}/rss/eztv",
            },
            timeout=15,
        )
        assert resp.status_code == 201
        return resp.json()["data"]["id"]

    @pytest.fixture
    def movie_channel(self):
        """Create a movie channel for metadata testing."""
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/channels",
            json={
                "name": "Filter Test - Movies",
                "url": f"{TEST_SERVER}/rss/movies",
            },
            timeout=15,
        )
        assert resp.status_code == 201
        return resp.json()["data"]["id"]

    def test_create_agent_with_resolution_filter(self, mikanani_channel):
        """Create an agent with a resolution filter."""
        # First create a downloader (needed for agent)
        dl_resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/downloaders",
            json={
                "name": "Test Downloader",
                "type": "transmission",
                "url": "http://transmission:9091/transmission/rpc",
                "download_dir": "/downloads/test",
            },
            timeout=10,
        )
        downloader_id = dl_resp.json()["data"]["id"]

        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/agents",
            json={
                "name": "1080p Anime Agent",
                "channel_id": mikanani_channel,
                "downloader_id": downloader_id,
                "content_type": "anime",
                "filters": [
                    {
                        "field": "resolution",
                        "operator": "eq",
                        "value": "1080p",
                        "priority": 10,
                        "is_required": True,
                    },
                    {
                        "field": "subtitle_group",
                        "operator": "eq",
                        "value": "LoliHouse",
                        "priority": 20,
                        "is_required": False,
                    },
                ],
            },
            timeout=15,
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["name"] == "1080p Anime Agent"
        assert data["content_type"] == "anime"
        assert len(data["filters"]) == 2

    def test_create_agent_with_imdb_metadata(self, movie_channel):
        """Create an agent with IMDB metadata source."""
        dl_resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/downloaders",
            json={
                "name": "Movie Downloader",
                "type": "transmission",
                "url": "http://transmission:9091/transmission/rpc",
                "download_dir": "/downloads/movies",
            },
            timeout=10,
        )
        downloader_id = dl_resp.json()["data"]["id"]

        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/agents",
            json={
                "name": "Movie Agent",
                "channel_id": movie_channel,
                "downloader_id": downloader_id,
                "content_type": "movie",
                "metadata_source": "imdb",
                "filters": [
                    {
                        "field": "resolution",
                        "operator": "eq",
                        "value": "1080p",
                        "priority": 10,
                        "is_required": True,
                    },
                ],
            },
            timeout=15,
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["metadata_source"] == "imdb"
        assert data["content_type"] == "movie"

    def test_create_eztv_agent_with_codec_filter(self, eztv_channel):
        """Create an agent for Western TV shows with codec filter."""
        dl_resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/downloaders",
            json={
                "name": "TV Downloader",
                "type": "transmission",
                "url": "http://transmission:9091/transmission/rpc",
                "download_dir": "/downloads/tv",
            },
            timeout=10,
        )
        downloader_id = dl_resp.json()["data"]["id"]

        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/agents",
            json={
                "name": "H264 TV Agent",
                "channel_id": eztv_channel,
                "downloader_id": downloader_id,
                "content_type": "tv",
                "filters": [
                    {
                        "field": "video_codec",
                        "operator": "contains",
                        "value": "H264",
                        "priority": 10,
                        "is_required": True,
                    },
                ],
            },
            timeout=15,
        )
        assert resp.status_code == 201

    def test_agent_list(self):
        """Verify agents are listed."""
        resp = httpx.get(f"{RSSRIPPLE}/api/v1/agents", timeout=10)
        assert resp.status_code == 200
        assert len(resp.json()["data"]) > 0

    def test_series_and_movies_crud(self):
        """Verify TVSeries and Movie CRUD endpoints work."""
        # Create a series
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/series",
            json={
                "title_cn": "黄泉使者",
                "title_en": "Daemons of the Shadow Realm",
                "aliases": ["Yomi no Tsugai"],
                "external_id": "tt12345678",
                "external_source": "imdb",
                "content_type": "anime",
            },
            timeout=10,
        )
        assert resp.status_code == 201
        series_id = resp.json()["data"]["id"]

        # Get series
        resp = httpx.get(f"{RSSRIPPLE}/api/v1/series/{series_id}", timeout=10)
        assert resp.status_code == 200
        assert resp.json()["data"]["title_cn"] == "黄泉使者"

        # Create a movie
        resp = httpx.post(
            f"{RSSRIPPLE}/api/v1/movies",
            json={
                "title_cn": "盗梦空间",
                "title_en": "Inception",
                "external_id": "tt1375666",
                "external_source": "imdb",
                "content_type": "movie",
            },
            timeout=10,
        )
        assert resp.status_code == 201
        movie_id = resp.json()["data"]["id"]

        # Get movie
        resp = httpx.get(f"{RSSRIPPLE}/api/v1/movies/{movie_id}", timeout=10)
        assert resp.status_code == 200
        assert resp.json()["data"]["external_id"] == "tt1375666"

    def test_downloader_has_download_dir(self):
        """Verify downloaders expose download_dir."""
        resp = httpx.get(f"{RSSRIPPLE}/api/v1/downloaders", timeout=10)
        assert resp.status_code == 200
        downloaders = resp.json()["data"]
        # At least one downloader should have download_dir
        has_dir = any(d.get("download_dir") for d in downloaders)
        assert has_dir, "No downloader has download_dir set"
