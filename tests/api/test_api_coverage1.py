"""Additional API coverage tests part 1: channels, agents."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _uuid():
    return str(uuid.uuid4())


@pytest.fixture
async def env(client):
    with patch(
        "app.api.v1.channels.validate_rss_url",
        AsyncMock(return_value=(True, "ok", 5, 5)),
    ):
        ch = await client.post("/api/v1/channels", json={
            "name": "C", "type": "rss_feed", "url": "https://x/rss",
            "fetch_interval": 1800, "metadata_source": "none",
        })
    dl = await client.post("/api/v1/downloaders", json={
        "name": "DL", "type": "transmission",
        "url": "http://127.0.0.1:9091/transmission/rpc",
    })
    a = await client.post("/api/v1/agents", json={
        "name": "A", "channel_id": ch.json()["data"]["id"],
        "downloader_id": dl.json()["data"]["id"], "scope_channel_wide": True,
    })
    return SimpleNamespace(
        ch_id=ch.json()["data"]["id"],
        dl_id=dl.json()["data"]["id"],
        aid=a.json()["data"]["id"],
    )


async def _make_resource(db_session_factory, ch_id, **kw):
    from app.models.file_resource import FileResource
    rid = _uuid()
    defaults = {
        "id": rid, "channel_id": ch_id, "guid": rid + "-g",
        "title_raw": kw.pop("title_raw", "[G] R - 01"),
        "search_title": "R",
        "torrent_url": f"magnet:?xt=urn:btih:{rid}",
        "parsed_at": datetime.now(UTC),
    }
    defaults.update(kw)
    async with db_session_factory() as s:
        s.add(FileResource(**defaults))
        await s.commit()
    return rid


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class TestChannelsMore:
    async def test_create_channel_duplicate_token(self, client, monkeypatch):
        from app.services import submission_guard as sg_mod
        fake = MagicMock()
        fake.consume = AsyncMock(return_value=False)
        monkeypatch.setattr(sg_mod, "submission_guard", fake)
        with patch(
            "app.api.v1.channels.validate_rss_url",
            AsyncMock(return_value=(True, "ok", 1, 1)),
        ):
            res = await client.post(
                "/api/v1/channels",
                headers={"X-Form-Token": "bad"},
                json={"name": "N", "type": "rss_feed", "url": "https://x/rss"},
            )
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "DUPLICATE_SUBMISSION"

    async def test_update_channel_404(self, client):
        res = await client.put("/api/v1/channels/nonexistent", json={"name": "X"})
        assert res.status_code == 404

    async def test_update_channel_token_duplicate(self, client, sample_channel, monkeypatch):
        from app.services import submission_guard as sg_mod
        fake = MagicMock()
        fake.consume = AsyncMock(return_value=False)
        monkeypatch.setattr(sg_mod, "submission_guard", fake)
        res = await client.put(
            f"/api/v1/channels/{sample_channel.id}",
            headers={"X-Form-Token": "x"},
            json={"name": "Renamed"},
        )
        assert res.status_code == 409

    async def test_analyze_channel_fetch_error(self, client, sample_channel):
        with patch(
            "app.api.v1.channels.get_raw_entries",
            AsyncMock(side_effect=RuntimeError("fetch fail")),
        ):
            res = await client.post(f"/api/v1/channels/{sample_channel.id}/analyze")
        assert res.status_code == 400

    async def test_analyze_channel_empty_feed(self, client, sample_channel):
        with patch("app.api.v1.channels.get_raw_entries", AsyncMock(return_value=[])):
            res = await client.post(f"/api/v1/channels/{sample_channel.id}/analyze")
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "EMPTY_FEED"

    async def test_analyze_stream_channel_404(self, client):
        res = await client.post("/api/v1/channels/nope/analyze-stream")
        assert res.status_code == 404

    async def test_analyze_stream_channel_fetch_error(self, client, sample_channel):
        with patch(
            "app.api.v1.channels.get_raw_entries",
            AsyncMock(side_effect=RuntimeError("x")),
        ):
            res = await client.post(f"/api/v1/channels/{sample_channel.id}/analyze-stream")
        assert res.status_code == 400

    async def test_analyze_stream_channel_empty(self, client, sample_channel):
        with patch("app.api.v1.channels.get_raw_entries", AsyncMock(return_value=[])):
            res = await client.post(f"/api/v1/channels/{sample_channel.id}/analyze-stream")
        assert res.status_code == 400

    async def test_analyze_url_stream_fetch_error(self, client):
        with patch(
            "app.api.v1.channels.get_raw_entries",
            AsyncMock(side_effect=RuntimeError("x")),
        ):
            res = await client.post("/api/v1/channels/analyze-url-stream",
                                    json={"url": "https://x/rss"})
        assert res.status_code == 400

    async def test_analyze_url_stream_empty(self, client):
        with patch("app.api.v1.channels.get_raw_entries", AsyncMock(return_value=[])):
            res = await client.post("/api/v1/channels/analyze-url-stream",
                                    json={"url": "https://x/rss"})
        assert res.status_code == 400

    async def test_generate_title_regex_fetch_error(self, client, sample_channel):
        with patch(
            "app.api.v1.channels.get_raw_entries",
            AsyncMock(side_effect=RuntimeError("x")),
        ):
            res = await client.post(f"/api/v1/channels/{sample_channel.id}/generate-title-regex")
        assert res.status_code == 400

    async def test_preview_feed_fetch_error(self, client):
        with patch(
            "app.api.v1.channels.get_raw_entries",
            AsyncMock(side_effect=RuntimeError("x")),
        ):
            res = await client.post("/api/v1/channels/preview-feed", json={"url": "https://x/rss"})
        assert res.status_code == 400

    async def test_preview_feed_without_field_mapping(self, client):
        with patch("app.api.v1.channels.get_raw_entries",
                   AsyncMock(return_value=[{"title": "[G] T - 01"}])):
            res = await client.post("/api/v1/channels/preview-feed",
                                    json={"url": "https://x/rss"})
        assert res.status_code == 200
        assert res.json()["data"]["parsed"] == []

    async def test_summarize_filters_no_matching_resources(self, client, sample_channel):
        res = await client.post(
            f"/api/v1/channels/{sample_channel.id}/summarize-filters",
            json={"resource_ids": [_uuid()]},
        )
        assert res.status_code == 200
        assert res.json()["data"]["filter_config"] is None

    async def test_fetch_status_404(self, client):
        res = await client.get("/api/v1/channels/nope/fetch-status")
        assert res.status_code == 404

    async def test_get_channel_returns_recent_resources(self, client, sample_channel, db_session_factory):
        await _make_resource(db_session_factory, sample_channel.id, title_raw="RRR")
        res = await client.get(f"/api/v1/channels/{sample_channel.id}")
        assert res.status_code == 200
        assert len(res.json()["data"]["recent_resources"]) >= 1

    async def test_validate_url_invalid(self, client):
        with patch("app.api.v1.channels.validate_rss_url",
                   AsyncMock(return_value=(False, "bad", 0, 0))):
            res = await client.post("/api/v1/channels/validate-url",
                                    json={"url": "https://x/bad"})
        assert res.status_code == 200
        assert res.json()["data"]["valid"] is False


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class TestAgentsMore:
    async def test_create_agent_with_works_in_body(self, client, env):
        s = await client.post("/api/v1/series", json={"title_cn": "剧", "title_en": "S"})
        m = await client.post("/api/v1/movies", json={"title_cn": "电影", "title_en": "M"})
        sid = s.json()["data"]["id"]
        mid = m.json()["data"]["id"]
        res = await client.post("/api/v1/agents", json={
            "name": "AW",
            "channel_id": env.ch_id, "downloader_id": env.dl_id,
            "scope_channel_wide": False,
            "works": [
                {"content_type": "tv", "series_id": sid},
                {"content_type": "movie", "movie_id": mid},
            ],
        })
        assert res.status_code == 201
        assert len(res.json()["data"]["works"]) == 2

    async def test_create_agent_skips_invalid_works(self, client, env):
        res = await client.post("/api/v1/agents", json={
            "name": "Skip",
            "channel_id": env.ch_id, "downloader_id": env.dl_id,
            "scope_channel_wide": False,
            "works": [
                {"content_type": "tv"},  # missing series_id → skipped
                {"content_type": "movie"},  # missing movie_id → skipped
            ],
        })
        assert res.status_code == 201
        assert res.json()["data"]["works"] == []

    async def test_create_agent_no_downloader_id(self, client, env):
        res = await client.post("/api/v1/agents", json={
            "name": "NoDL", "channel_id": env.ch_id, "scope_channel_wide": True,
        })
        assert res.status_code == 201

    async def test_get_agent_404(self, client):
        r = await client.get("/api/v1/agents/nope")
        assert r.status_code == 404

    async def test_update_agent_404(self, client):
        r = await client.put("/api/v1/agents/nope", json={"name": "X"})
        assert r.status_code == 404

    async def test_delete_agent_404(self, client):
        r = await client.delete("/api/v1/agents/nope")
        assert r.status_code == 404

    async def test_run_404(self, client):
        r = await client.post("/api/v1/agents/nope/run")
        assert r.status_code == 404

    async def test_run_status_404(self, client):
        r = await client.get("/api/v1/agents/nope/run-status")
        assert r.status_code == 404

    async def test_test_filters_404(self, client):
        r = await client.post("/api/v1/agents/nope/test-filters")
        assert r.status_code == 404

    async def test_suggestions_404(self, client):
        r = await client.get("/api/v1/agents/nope/suggestions")
        assert r.status_code == 404

    async def test_list_works_404(self, client):
        r = await client.get("/api/v1/agents/nope/works")
        assert r.status_code == 404

    async def test_create_work_404(self, client):
        r = await client.post("/api/v1/agents/nope/works",
                              json={"content_type": "tv", "series_id": _uuid()})
        assert r.status_code == 404

    async def test_create_work_movie_required(self, client, env):
        r = await client.post(f"/api/v1/agents/{env.aid}/works",
                              json={"content_type": "movie"})
        assert r.status_code == 422

    async def test_update_work_404(self, client, env):
        r = await client.put(f"/api/v1/agents/{env.aid}/works/nope",
                             json={"enable_episode_dedup": False})
        assert r.status_code == 404

    async def test_delete_work_404(self, client, env):
        r = await client.delete(f"/api/v1/agents/{env.aid}/works/nope")
        assert r.status_code == 404

    async def test_suggestions_with_unlinked(self, client, env, db_session_factory):
        await _make_resource(db_session_factory, env.ch_id, title_raw="Unknown Show E01")
        r = await client.get(f"/api/v1/agents/{env.aid}/suggestions")
        assert r.status_code == 200
        assert "suggestions" in r.json()["data"]

    async def test_test_filters_no_config(self, client, env, db_session_factory):
        rid = await _make_resource(db_session_factory, env.ch_id, title_raw="R")
        r = await client.post(f"/api/v1/agents/{env.aid}/test-filters",
                              json={"resource_ids": [rid]})
        assert r.status_code == 200
        assert r.json()["data"]["passed"] == 1
