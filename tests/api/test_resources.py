"""API tests for FileResource endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _uuid():
    return str(uuid.uuid4())


async def _make_resource(db_session_factory, channel_id, **overrides):
    from datetime import timezone
    from app.models.file_resource import FileResource
    rid = overrides.pop("id", _uuid())
    defaults = dict(
        id=rid,
        channel_id=channel_id,
        guid=_uuid(),
        title_raw="[Group] Show - 01 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
        search_title="Show",
        parsed_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    async with db_session_factory() as s:
        r = FileResource(**defaults)
        s.add(r)
        await s.commit()
    return rid


class TestResourceList:
    async def test_list_resources(self, client, sample_channel, db_session_factory):
        await _make_resource(db_session_factory, sample_channel.id, title_raw="R1")
        res = await client.get(f"/api/v1/channels/{sample_channel.id}/resources")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] >= 1

    async def test_list_grouped(self, client, sample_channel, db_session_factory):
        await _make_resource(db_session_factory, sample_channel.id, title_raw="R-group")
        res = await client.get(f"/api/v1/channels/{sample_channel.id}/resources?grouped=true")
        assert res.status_code == 200
        assert "groups" in res.json()["data"]
        groups = res.json()["data"]["groups"]
        assert any(g["type"] == "unknown" for g in groups)

    async def test_get_resource(self, client, sample_channel, db_session_factory):
        rid = await _make_resource(db_session_factory, sample_channel.id, title_raw="Rget")
        res = await client.get(f"/api/v1/resources/{rid}")
        assert res.status_code == 200
        assert res.json()["data"]["id"] == rid

    async def test_get_resource_404(self, client):
        res = await client.get("/api/v1/resources/nope")
        assert res.status_code == 404

    async def test_list_resources_404(self, client):
        res = await client.get("/api/v1/channels/nope/resources")
        assert res.status_code == 404


class TestChannelFieldValues:
    """Autocomplete endpoint used by the Filter DSL editor."""

    async def test_top_string_values_by_frequency(
        self, client, sample_channel, db_session_factory,
    ):
        # Two 1080p rows + one 720p — expect 1080p first.
        for _ in range(2):
            await _make_resource(
                db_session_factory, sample_channel.id, resolution="1080p",
            )
        await _make_resource(
            db_session_factory, sample_channel.id, resolution="720p",
        )
        res = await client.get(
            f"/api/v1/channels/{sample_channel.id}/field-values"
            "?field=resolution"
        )
        assert res.status_code == 200
        values = res.json()["data"]
        assert values[0] == "1080p"
        assert set(values) == {"1080p", "720p"}

    async def test_prefix_filter(self, client, sample_channel, db_session_factory):
        await _make_resource(db_session_factory, sample_channel.id, resolution="1080p")
        await _make_resource(db_session_factory, sample_channel.id, resolution="2160p")
        res = await client.get(
            f"/api/v1/channels/{sample_channel.id}/field-values"
            "?field=resolution&q=10"
        )
        assert res.status_code == 200
        assert res.json()["data"] == ["1080p"]

    async def test_subtitle_langs_unnest(self, client, sample_channel, db_session_factory):
        await _make_resource(
            db_session_factory, sample_channel.id,
            subtitle_langs=["zh-CN", "zh-TW"],
        )
        await _make_resource(
            db_session_factory, sample_channel.id, subtitle_langs=["zh-CN", "ja"],
        )
        res = await client.get(
            f"/api/v1/channels/{sample_channel.id}/field-values"
            "?field=subtitle_langs"
        )
        assert res.status_code == 200
        values = res.json()["data"]
        # zh-CN appears twice → sorts first; the other tags follow.
        assert values[0] == "zh-CN"
        assert set(values) == {"zh-CN", "zh-TW", "ja"}

    async def test_unsupported_field_rejected(
        self, client, sample_channel, db_session_factory,
    ):
        res = await client.get(
            f"/api/v1/channels/{sample_channel.id}/field-values"
            "?field=file_size"
        )
        assert res.status_code == 422

    async def test_unknown_channel_404(self, client):
        res = await client.get(
            "/api/v1/channels/nope/field-values?field=resolution"
        )
        assert res.status_code == 404


class TestResourceMetadata:
    async def test_metadata_404(self, client):
        res = await client.get("/api/v1/resources/nope/metadata")
        assert res.status_code == 404

    async def test_metadata_unlinked_triggers_match(self, client, sample_channel, db_session_factory):
        rid = await _make_resource(db_session_factory, sample_channel.id, title_raw="RAW-unlinked")
        res = await client.get(f"/api/v1/resources/{rid}/metadata")
        assert res.status_code == 200
        d = res.json()["data"]
        # Not linked because no matches and metadata_source=none
        assert d["series_id"] is None
        assert d["movie_id"] is None

    async def test_metadata_linked_returns_entity(self, client, sample_channel, db_session_factory):
        # create a series and link resource directly
        from app.models.series import TVSeries
        sid = _uuid()
        async with db_session_factory() as s:
            series = TVSeries(id=sid, title_cn="剧", title_en="LinkedShow", content_type="tv")
            s.add(series)
            await s.commit()
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            title_raw="RAW-linked", series_id=sid, metadata_matched_at=datetime.now(UTC),
        )
        res = await client.get(f"/api/v1/resources/{rid}/metadata")
        assert res.status_code == 200
        assert res.json()["data"]["series_id"] == sid


class TestResourceSearchLink:
    async def test_search_404(self, client):
        res = await client.post("/api/v1/resources/nope/metadata/search",
                                json={"search_title": "x", "content_type": "tv"})
        assert res.status_code == 404

    async def test_search_returns_results(self, client, sample_channel, db_session_factory):
        rid = await _make_resource(db_session_factory, sample_channel.id, title_raw="RAW-s")
        fake = [{"content_type": "tv", "title_cn": "候选", "title_en": "Cand",
                 "original_title": "Cand", "external_id": "cid",
                 "external_source": "llm_search", "description": "d", "poster_url": None}]
        with patch(
            "app.services.metadata_service.search_metadata_via_llm",
            new_callable=AsyncMock, return_value=fake,
        ):
            res = await client.post(f"/api/v1/resources/{rid}/metadata/search",
                                    json={"search_title": "unk", "content_type": "tv"})
        assert res.status_code == 200
        assert len(res.json()["data"]["results"]) == 1

    async def test_search_llm_error_returns_502(self, client, sample_channel, db_session_factory):
        rid = await _make_resource(db_session_factory, sample_channel.id, title_raw="RAW-e")
        with patch(
            "app.services.metadata_service.search_metadata_via_llm",
            new_callable=AsyncMock, side_effect=RuntimeError("LLM fail"),
        ):
            res = await client.post(f"/api/v1/resources/{rid}/metadata/search",
                                    json={"search_title": "x", "content_type": "tv"})
        assert res.status_code == 502

    async def test_link_creates_series(self, client, sample_channel, db_session_factory):
        rid = await _make_resource(db_session_factory, sample_channel.id, title_raw="RAW-link")
        sel = {
            "content_type": "tv",
            "title_cn": "新剧", "title_en": "New Show", "original_title": "New Show",
            "external_id": "ext-new", "external_source": "manual",
        }
        with patch(
            "app.services.metadata_service.download_and_cache_poster",
            new_callable=AsyncMock, return_value=None,
        ):
            res = await client.put(f"/api/v1/resources/{rid}/metadata/link",
                                   json={"selected_result": sel})
        assert res.status_code == 200
        assert res.json()["data"]["series_id"] is not None

    async def test_link_404(self, client):
        res = await client.put("/api/v1/resources/nope/metadata/link",
                               json={"selected_result": {"content_type": "tv", "external_id": "x"}})
        assert res.status_code == 404
