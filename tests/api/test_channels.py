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

    async def test_required_metadata_fields_crud(self, client, sample_channel):
        from app.services.required_fields import (
            REQUIRED_FIELD_CATALOG,
            normalize_required_fields,
        )

        baseline = normalize_required_fields([])

        # Default: locked baseline (mandatory, never unrestricted).
        res = await client.get(f"/api/v1/channels/{sample_channel.id}")
        assert res.json()["data"]["required_metadata_fields"] == baseline

        # Set a selection: duplicates drop, baseline force-included,
        # canonical catalog order applied.
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"required_metadata_fields": ["rating", "genre", "rating"]},
        )
        assert res.status_code == 200
        expected = [
            k for k in REQUIRED_FIELD_CATALOG
            if k in set(baseline) | {"rating", "genre"}
        ]
        assert res.json()["data"]["required_metadata_fields"] == expected

        # Add-only policy: removing any saved key → 422. ("year" rides in with
        # the locked baseline now, so an opt-in key exercises the removal.)
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"required_metadata_fields": ["rating"]},
        )
        assert res.status_code == 422
        assert "genre" in res.json()["error"]["message"]

        # Adding new keys on top of the saved ones is fine.
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={
                "required_metadata_fields": expected + ["genre"],
            },
        )
        assert res.status_code == 200
        assert "genre" in res.json()["data"]["required_metadata_fields"]

        # A legacy channel that already saved optional title_cn keeps the
        # add-only requirement and cannot remove it.
        with_title_cn = list(res.json()["data"]["required_metadata_fields"]) + ["title_cn"]
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"required_metadata_fields": with_title_cn},
        )
        assert res.status_code == 200
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"required_metadata_fields": [
                key for key in with_title_cn if key != "title_cn"
            ]},
        )
        assert res.status_code == 422
        assert "title_cn" in res.json()["error"]["message"]

        # Unknown catalog key → 422.
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"required_metadata_fields": ["bogus"]},
        )
        assert res.status_code == 422

        # Explicit null is rejected — there is no "unrestricted" state.
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"required_metadata_fields": None},
        )
        assert res.status_code == 422

    async def test_required_field_catalog_endpoint(self, client):
        from app.services.required_fields import REQUIRED_FIELD_CATALOG

        res = await client.get("/api/v1/channels/required-field-catalog")
        assert res.status_code == 200
        data = res.json()["data"]
        # Sections order work-type grouping first, then cross-cutting groups.
        assert data["sections"] == ["base", "tv", "pack", "release", "work"]
        keys = [f["key"] for f in data["fields"]]
        # Catalog covers every DSL field (resource-level keys under their own
        # name; work fields paired under semantic keys).
        assert keys == list(REQUIRED_FIELD_CATALOG)
        rating = next(f for f in data["fields"] if f["key"] == "rating")
        assert rating["dsl_fields"] == ["series.rating", "movie.rating"]
        assert rating["locked"] is False
        assert rating["section"] == "work"
        resource_collection = next(
            f for f in data["fields"] if f["key"] == "resource_collection"
        )
        assert resource_collection["dsl_fields"] == ["collection"]
        assert resource_collection["section"] == "pack"
        assert resource_collection["applies_to"] == ["franchise"]
        season = next(f for f in data["fields"] if f["key"] == "season")
        assert season["lock"] == "tv"
        episode = next(f for f in data["fields"] if f["key"] == "episode")
        assert episode["lock"] == "tv_single"
        year = next(f for f in data["fields"] if f["key"] == "year")
        assert year["locked"] is True
        assert year["lock"] == "always"
        locked_keys = {f["key"] for f in data["fields"] if f["locked"]}
        assert locked_keys == {
            "search_title", "content_type", "is_batch", "year", "is_anime",
            "season", "episode", "episode_start", "episode_end",
            "resource_collection",
        }

    async def test_update_channel_status_not_editable(self, client, sample_channel):
        # status is system-managed; the edit form must not be able to set it.
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"name": "X", "status": "inactive"},
        )
        assert res.status_code == 200
        # status stays 'active' (the channel's existing value), not 'inactive'
        assert res.json()["data"]["status"] == "active"

    async def test_update_channel_reschedules_to_apply_new_settings(
        self, client, sample_channel, monkeypatch
    ):
        # Editing fetch_interval / metadata_source must reset the background
        # task so the new settings take effect.
        from app.services import scheduler as sched_mod

        captured: dict = {}

        def fake_reschedule(ch):
            captured["channel_id"] = ch.id
            captured["fetch_interval"] = ch.fetch_interval
            captured["metadata_source"] = ch.metadata_source

        monkeypatch.setattr(sched_mod, "reschedule_channel", fake_reschedule)
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"fetch_interval": 600, "metadata_source": "tmdb"},
        )
        assert res.status_code == 200
        assert captured.get("channel_id") == sample_channel.id
        assert captured.get("fetch_interval") == 600
        assert captured.get("metadata_source") == "tmdb"

    async def test_delete_channel(self, client, sample_channel):
        res = await client.delete(f"/api/v1/channels/{sample_channel.id}")
        assert res.status_code == 200
        assert res.json()["data"]["deleted"] is True
        # After delete, 404
        res2 = await client.get(f"/api/v1/channels/{sample_channel.id}")
        assert res2.status_code == 404

    async def test_create_channel_with_default_is_anime(self, client):
        with patch(
            "app.api.v1.channels.validate_rss_url",
            AsyncMock(return_value=(True, "ok", 5, 5)),
        ):
            res = await client.post(
                "/api/v1/channels", json=_channel_payload(default_is_anime=True)
            )
        assert res.status_code == 201
        assert res.json()["data"]["default_is_anime"] is True

    async def test_update_channel_default_is_anime_immutable(
        self, client, sample_channel
    ):
        # sample_channel has the default (False); flipping it must 422.
        assert sample_channel.default_is_anime is False
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"default_is_anime": True},
        )
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"
        # Re-submitting the SAME value is a no-op, not an error (the edit
        # form always carries the field).
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"default_is_anime": False},
        )
        assert res.status_code == 200


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
        assert res.json()["data"]["global_filter_config"] is None
        assert res.json()["data"]["works"] == []

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
        data = res.json()["data"]
        # Unlinked resource: no work subscription, but its uniform fields are global.
        assert data["unlinked_count"] == 1
        assert data["works"] == []
        conds = data["global_filter_config"]["conditions"]
        assert {c["field"] for c in conds} == {"subtitle_group", "resolution"}

    async def test_summarize_filters_splits_global_and_work_overrides(
        self, client, sample_channel, db_session_factory
    ):
        """Works become subscriptions; globally-common fields go to the global
        filter; per-work uniform fields go to that work's overrides."""
        from app.models.file_resource import FileResource
        from app.models.series import TVSeries

        s1_id, s2_id = str(uuid.uuid4()), str(uuid.uuid4())
        rids = [str(uuid.uuid4()) for _ in range(4)]
        async with db_session_factory() as s:
            s.add(TVSeries(id=s1_id, title_cn="剧A", content_type="tv"))
            s.add(TVSeries(id=s2_id, title_cn="剧B", content_type="tv"))
            # Work A: all ANi; Work B: all LoliHouse; all 1080p globally.
            for i, (sid, grp) in enumerate([(s1_id, "ANi"), (s1_id, "ANi"), (s2_id, "LoliHouse"), (s2_id, "LoliHouse")]):
                s.add(FileResource(
                    id=rids[i], channel_id=sample_channel.id, guid=rids[i] + "-g",
                    title_raw=f"T{i}", subtitle_group=grp, resolution="1080p",
                    series_id=sid, torrent_url="magnet:?xt=urn:btih:x",
                ))
            await s.commit()

        res = await client.post(
            f"/api/v1/channels/{sample_channel.id}/summarize-filters",
            json={"resource_ids": rids},
        )
        assert res.status_code == 200
        data = res.json()["data"]

        # resolution is common across all -> global only.
        gconds = data["global_filter_config"]["conditions"]
        assert gconds == [{"field": "resolution", "operator": "eq", "value": "1080p"}]

        assert len(data["works"]) == 2
        by_series = {w["series_id"]: w for w in data["works"]}
        assert by_series[s1_id]["resource_count"] == 2
        # subtitle_group differs between works -> per-work overrides, not global.
        assert by_series[s1_id]["filter_overrides"]["conditions"] == [
            {"field": "subtitle_group", "operator": "eq", "value": "ANi"}
        ]
        assert by_series[s2_id]["filter_overrides"]["conditions"] == [
            {"field": "subtitle_group", "operator": "eq", "value": "LoliHouse"}
        ]
        assert by_series[s1_id]["title"] == "剧A"


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
        # Channel config is restricted to the channel-source architecture.
        assert set(values) == {"wikipedia", "tmdb", "bangumi"}
        assert data["default"] == "wikipedia"
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

    async def test_create_channel_rejects_deprecated_sources(self, client):
        # exa/jina/local/combined are deprecated as channel sources (P1).
        with patch(
            "app.api.v1.channels.validate_rss_url",
            AsyncMock(return_value=(True, "ok", 3, 3)),
        ):
            for legacy in ("exa", "jina", "local", "combined"):
                res = await client.post(
                    "/api/v1/channels",
                    json=_channel_payload(metadata_source=legacy),
                )
                assert res.status_code == 422, legacy

    async def test_update_channel_rejects_deprecated_source(self, client, sample_channel):
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"metadata_source": "exa"},
        )
        assert res.status_code == 422

    async def test_channel_fallback_sources_roundtrip(self, client, sample_channel):
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"metadata_fallback_sources": ["bangumi", "tmdb"]},
        )
        assert res.status_code == 200
        assert res.json()["data"]["metadata_fallback_sources"] == ["bangumi", "tmdb"]
        got = await client.get(f"/api/v1/channels/{sample_channel.id}")
        assert got.json()["data"]["metadata_fallback_sources"] == ["bangumi", "tmdb"]
        # Empty list = fallback disabled (distinct from None = default order).
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"metadata_fallback_sources": []},
        )
        assert res.status_code == 200
        assert res.json()["data"]["metadata_fallback_sources"] == []

    async def test_channel_rejects_unknown_fallback_source(self, client, sample_channel):
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            json={"metadata_fallback_sources": ["bangumi", "eiga"]},
        )
        assert res.status_code == 422


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


class TestChannelDeleteCascade:
    """Regression: deleting a channel that owns download tasks used to 500
    with IntegrityError — the ORM nullified download_tasks.file_resource_id
    (NOT NULL) instead of deleting the tasks."""

    async def test_delete_channel_with_download_tasks(self, client, sample_channel, db_session_factory):
        import uuid as _uuid_mod
        from datetime import UTC, datetime

        from app.models.agent import Agent
        from app.models.download_task import DownloadTask
        from app.models.downloader import DownloaderInstance
        from app.models.file_resource import FileResource

        def _u():
            return str(_uuid_mod.uuid4())

        async with db_session_factory() as s:
            dl = DownloaderInstance(
                id=_u(), name="DL", type="transmission",
                url="http://127.0.0.1:9091/transmission/rpc", download_dir="/downloads",
            )
            agent = Agent(
                id=_u(), name="A", channel_id=sample_channel.id, downloader_id=dl.id,
                scope_channel_wide=True,
            )
            res = FileResource(
                id=_u(), channel_id=sample_channel.id, guid=_u(), title_raw="[G] T - 01",
                search_title="T", torrent_url=f"magnet:?xt=urn:btih:{_u()}",
                parsed_at=datetime.now(UTC),
            )
            s.add_all([dl, agent, res])
            await s.flush()
            s.add(DownloadTask(
                id=_u(), agent_id=agent.id, file_resource_id=res.id, downloader_id=dl.id,
                status="pending", download_dir="/downloads/A",
            ))
            await s.commit()

        res_del = await client.delete(f"/api/v1/channels/{sample_channel.id}")
        assert res_del.status_code == 200, res_del.text
        assert res_del.json()["data"]["deleted"] is True

        # Download tasks must be gone too (cascaded), not left with a nulled FK.
        from sqlalchemy import select as _select
        async with db_session_factory() as s:
            remaining = (await s.execute(_select(DownloadTask))).scalars().all()
        assert remaining == []
