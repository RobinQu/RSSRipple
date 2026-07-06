"""API tests for channel endpoints."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch


def _channel_payload(**overrides):
    base = {
        "name": "Test Channel",
        "type": "rss_feed",
        "url": "https://example.com/rss",
        "fetch_interval": 1800,
        "field_mapping": {
            "list_locator": {"source": "entries"},
            "field_mappings": {"torrent_url": {"source": "link"}},
        },
        "metadata_agent_enabled": False,
    }
    base.update(overrides)
    return base


class TestChannelsCRUD:
    async def test_create_channel(self, client):
        with patch(
            "app.api.v1.channels.validate_rss_url",
            AsyncMock(return_value=(True, "ok", 5, 5)),
        ):
            res = await client.post("/api/v1/channels", json=_channel_payload())
        assert res.status_code == 201
        data = res.json()
        assert data["success"] is True
        assert data["data"]["name"] == "Test Channel"
        assert data["meta"]["feed_items"] == 5

    async def test_create_channel_invalid_feed(self, client):
        from unittest.mock import AsyncMock
        with patch(
            "app.clients.rss_parser.validate_rss_url",
            AsyncMock(return_value=(False, "bad", 0, 0)),
        ):
            res = await client.post("/api/v1/channels", json=_channel_payload())
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "INVALID_FEED"

    async def test_list_channels(self, client, sample_channel):
        res = await client.get("/api/v1/channels")
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["meta"]["total"] >= 1

    async def test_get_channel(self, client, sample_channel):
        res = await client.get(f"/api/v1/channels/{sample_channel.id}")
        assert res.status_code == 200
        assert res.json()["data"]["id"] == sample_channel.id

    async def test_get_channel_404(self, client):
        res = await client.get("/api/v1/channels/does-not-exist")
        assert res.status_code == 404

    async def test_update_channel(self, client, sample_channel):
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"name": "Renamed"},
        )
        assert res.status_code == 200
        assert res.json()["data"]["name"] == "Renamed"

    async def test_delete_channel(self, client, sample_channel):
        res = await client.delete(f"/api/v1/channels/{sample_channel.id}")
        assert res.status_code == 200
        assert res.json()["data"]["deleted"] is True
        # After delete, 404
        res2 = await client.get(f"/api/v1/channels/{sample_channel.id}")
        assert res2.status_code == 404


class TestChannelActions:
    async def test_fetch_enqueues_job(self, client, sample_channel):
        res = await client.post(f"/api/v1/channels/{sample_channel.id}/fetch")
        assert res.status_code == 200
        assert res.json()["success"] is True

    async def test_fetch_404(self, client):
        res = await client.post("/api/v1/channels/nope/fetch")
        assert res.status_code == 404

    async def test_fetch_already_running_returns_409(self, client, sample_channel, monkeypatch):
        from app.services import task_queue as tq_mod
        fake = MagicMock()
        fake.enqueue = AsyncMock(return_value=None)
        fake.status = AsyncMock(return_value={"status": "running"})
        monkeypatch.setattr(tq_mod, "task_queue", fake)
        res = await client.post(f"/api/v1/channels/{sample_channel.id}/fetch")
        assert res.status_code == 409

    async def test_fetch_status(self, client, sample_channel):
        res = await client.get(f"/api/v1/channels/{sample_channel.id}/fetch-status")
        assert res.status_code == 200
        assert "status" in res.json()["data"]

    async def test_validate_url(self, client):
        with patch(
            "app.api.v1.channels.validate_rss_url",
            AsyncMock(return_value=(True, "ok", 10, 8)),
        ):
            res = await client.post(
                "/api/v1/channels/validate-url", json={"url": "https://x/rss"}
            )
        assert res.status_code == 200
        assert res.json()["data"]["valid"] is True

    async def test_analyze_channel(self, client, sample_channel):
        from unittest.mock import AsyncMock
        with patch(
            "app.api.v1.channels.get_raw_entries",
            AsyncMock(return_value=[{"title": "[G] T - 01"}]),
        ), patch(
            "app.api.v1.channels.analyze_feed",
            AsyncMock(return_value={
                "field_mapping": {
                    "list_locator": {"source": "entries"},
                    "field_mappings": {"torrent_url": {"source": "link"}},
                },
                "sample_results": [],
                "confidence": "high",
            }),
        ):
            res = await client.post(f"/api/v1/channels/{sample_channel.id}/analyze")
        assert res.status_code == 200
        body = res.json()
        assert body["success"] is True
        assert body["data"]["field_mapping"] is not None

    async def test_analyze_channel_404(self, client):
        res = await client.post("/api/v1/channels/nope/analyze")
        assert res.status_code == 404

    async def test_preview_feed(self, client):
        from unittest.mock import AsyncMock
        with patch(
            "app.api.v1.channels.get_raw_entries",
            AsyncMock(return_value=[{"title": "[G] T - 01"}]),
        ):
            res = await client.post("/api/v1/channels/preview-feed",
                                    json={"url": "https://x/rss"})
        assert res.status_code == 200
        assert "entries" in res.json()["data"]

    async def test_preview_feed_with_field_mapping(self, client):
        from unittest.mock import AsyncMock
        with patch(
            "app.api.v1.channels.get_raw_entries",
            AsyncMock(return_value=[{"title": "[G] T - 01"}]),
        ):
            res = await client.post("/api/v1/channels/preview-feed", json={
                "url": "https://x/rss",
                "field_mapping": {
                    "list_locator": {"source": "entries"},
                    "field_mappings": {"torrent_url": {"source": "link"}},
                },
            })
        assert res.status_code == 200

    async def test_summarize_filters_empty(self, client, sample_channel):
        res = await client.post(
            f"/api/v1/channels/{sample_channel.id}/summarize-filters",
            json={"resource_ids": []},
        )
        assert res.status_code == 200
        assert res.json()["data"]["filter_config"] is None

    async def test_summarize_filters_with_resources(self, client, sample_channel, db_session_factory):
        # Create resources directly
        from app.models.file_resource import FileResource
        rid = str(uuid.uuid4())
        async with db_session_factory() as s:
            r = FileResource(
                id=rid, channel_id=sample_channel.id, guid=rid + "-g",
                title_raw="T", subtitle_group="GroupX", resolution="1080p",
                torrent_url="magnet:?xt=urn:btih:x",
            )
            s.add(r)
            await s.commit()
        res = await client.post(
            f"/api/v1/channels/{sample_channel.id}/summarize-filters",
            json={"resource_ids": [rid]},
        )
        assert res.status_code == 200


class TestChannelResources:
    async def test_list_resources_flat(self, client, sample_channel):
        res = await client.get(f"/api/v1/channels/{sample_channel.id}/resources")
        assert res.status_code == 200
        assert res.json()["success"] is True

    async def test_list_resources_grouped(self, client, sample_channel):
        res = await client.get(
            f"/api/v1/channels/{sample_channel.id}/resources?grouped=true"
        )
        assert res.status_code == 200
        body = res.json()
        assert "groups" in body["data"]

    async def test_list_resources_404(self, client):
        res = await client.get("/api/v1/channels/nope/resources")
        assert res.status_code == 404


class TestFormToken:
    async def test_get_form_token(self, client):
        res = await client.get("/api/v1/channels/form-token")
        assert res.status_code == 200
        assert res.json()["data"]["token"] == "test-token"


class TestMetadataSources:
    async def test_list_metadata_sources(self, client):
        res = await client.get("/api/v1/channels/metadata-sources")
        assert res.status_code == 200
        data = res.json()["data"]
        values = [s["value"] for s in data["sources"]]
        # All four external sources are exposed.
        assert {"exa", "jina", "wikipedia", "tmdb"} <= set(values)
        assert data["default"] == "exa"
        # Each entry carries the availability flags.
        for s in data["sources"]:
            assert set(s.keys()) >= {"value", "label", "available", "enabled", "configured"}
            assert s["available"] == (s["enabled"] and s["configured"])

    async def test_create_channel_with_metadata_source(self, client):
        with patch(
            "app.api.v1.channels.validate_rss_url",
            AsyncMock(return_value=(True, "ok", 3, 3)),
        ):
            res = await client.post(
                "/api/v1/channels",
                json=_channel_payload(metadata_source="wikipedia"),
            )
        assert res.status_code == 201
        data = res.json()["data"]
        assert data["metadata_source"] == "wikipedia"
        # Round-trips through GET.
        got = await client.get(f"/api/v1/channels/{data['id']}")
        assert got.json()["data"]["metadata_source"] == "wikipedia"

    async def test_create_channel_rejects_invalid_source(self, client):
        with patch(
            "app.api.v1.channels.validate_rss_url",
            AsyncMock(return_value=(True, "ok", 3, 3)),
        ):
            res = await client.post(
                "/api/v1/channels",
                json=_channel_payload(metadata_source="bogus"),
            )
        assert res.status_code == 422

    async def test_update_channel_metadata_source(self, client, sample_channel):
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"metadata_source": "tmdb"},
        )
        assert res.status_code == 200
        assert res.json()["data"]["metadata_source"] == "tmdb"


class TestCreateAutoFetch:
    async def test_create_enqueues_initial_fetch(self, client):
        """Creating a channel auto-triggers a fetch_channel job."""
        with patch(
            "app.api.v1.channels.validate_rss_url",
            AsyncMock(return_value=(True, "ok", 5, 5)),
        ):
            res = await client.post("/api/v1/channels", json=_channel_payload())
        assert res.status_code == 201
        # The fake queue returns a truthy job dict → fetch_triggered is True.
        assert res.json()["meta"]["fetch_triggered"] is True

    async def test_create_still_succeeds_when_fetch_dedup(self, client, monkeypatch):
        """If the initial fetch is deduped (None), create still succeeds."""
        from app.services import task_queue as tq_mod

        fake = MagicMock()
        fake.enqueue = AsyncMock(return_value=None)  # already-running / dedup
        fake.status = AsyncMock(return_value=None)
        monkeypatch.setattr(tq_mod, "task_queue", fake)
        with patch(
            "app.api.v1.channels.validate_rss_url",
            AsyncMock(return_value=(True, "ok", 5, 5)),
        ):
            res = await client.post("/api/v1/channels", json=_channel_payload())
        assert res.status_code == 201
        assert res.json()["meta"]["fetch_triggered"] is False
        fake.enqueue.assert_awaited_once()
        assert fake.enqueue.call_args.args[0] == "fetch_channel"
