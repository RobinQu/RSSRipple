"""Integration test: end-to-end RSS → parse → Transmission flow.

Requires docker-compose with Transmission running.
Run with: docker-compose -f docker-compose.test.yml up --build
"""

import pytest


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
        })
        assert ch_res.status_code == 201
        ch_id = ch_res.json()["data"]["id"]

        # 4. Create agent with filters
        ag_res = await client.post("/api/v1/agents", json={
            "name": "Test Agent",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "llm_enabled": False,
            "filters": [
                {"field": "resolution", "operator": "eq", "value": "1080p", "priority": 10, "is_required": True},
            ],
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
