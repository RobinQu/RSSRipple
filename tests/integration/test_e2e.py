"""Integration test: end-to-end RSS → parse → Transmission flow.

Requires docker-compose with Transmission running.
Run with: docker-compose -f docker-compose.test.yml up --build
"""

import pytest

TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


@pytest.mark.integration
class TestE2EFlow:
    """End-to-end integration tests (requires running services)."""

    @pytest.mark.asyncio
    async def test_full_flow(self, client):
        """Test: create channel → fetch RSS → create agent → filter → download task."""
        # 1. Create downloader (requires running Transmission)
        dl_res = await client.post("/api/v1/downloaders", json={
            "name": "Test TR",
            "type": "transmission",
            "url": "http://transmission:9091/transmission/rpc",
            "download_dir": "/downloads",
        })
        assert dl_res.status_code == 201
        dl_id = dl_res.json()["data"]["id"]

        # 2. Test downloader connection
        test_res = await client.post(f"/api/v1/downloaders/{dl_id}/test")
        assert test_res.status_code == 200

        # 3. Create channel
        ch_res = await client.post("/api/v1/channels", json={
            "name": "Test Feed",
            "url": "http://mock-rss:8080/feed.xml",  # Mock RSS server
            "fetch_interval": 60,
            "field_mapping": TEST_FIELD_MAPPING,
        })
        assert ch_res.status_code == 201
        ch_id = ch_res.json()["data"]["id"]

        # 4. Create agent with filter_config DSL
        ag_res = await client.post("/api/v1/agents", json={
            "name": "Test Agent",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "llm_enabled": False,
            "scope_channel_wide": True,
            "conflict_resolution": "auto",
            "filter_config": {
                "combinator": "and",
                "conditions": [
                    {"field": "resolution", "operator": "eq", "value": "1080p"},
                ],
            },
        })
        assert ag_res.status_code == 201
        ag_id = ag_res.json()["data"]["id"]

        # 5. Trigger fetch
        fetch_res = await client.post(f"/api/v1/channels/{ch_id}/fetch")
        assert fetch_res.status_code == 200

        # 6. Verify resources were created
        res_res = await client.get(f"/api/v1/channels/{ch_id}/resources")
        assert res_res.status_code == 200

        # 7. Trigger agent
        run_res = await client.post(f"/api/v1/agents/{ag_id}/run")
        assert run_res.status_code == 200
