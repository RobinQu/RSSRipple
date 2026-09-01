"""API tests for FileResource endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest


def _uuid():
    return str(uuid.uuid4())


async def _make_resource(db_session_factory, channel_id, **overrides):
    from app.models.file_resource import FileResource
    rid = overrides.pop("id", _uuid())
    defaults = dict(
        id=rid,
        channel_id=channel_id,
        guid=_uuid(),
        title_raw="[Group] Show - 01 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
        search_title="Show",
        parsed_at=datetime.now(UTC),
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

    async def test_list_marks_resources_with_any_download_task(
        self, client, sample_channel, sample_downloader, db_session_factory
    ):
        from app.models.download_task import DownloadTask

        with_task = await _make_resource(
            db_session_factory, sample_channel.id, title_raw="task-created"
        )
        without_task = await _make_resource(
            db_session_factory, sample_channel.id, title_raw="no-task"
        )
        async with db_session_factory() as session:
            session.add(DownloadTask(
                id=_uuid(), agent_id=None, file_resource_id=with_task,
                downloader_id=sample_downloader.id, download_dir="/d",
                status="pending",
            ))
            await session.commit()

        for suffix in ("", "?grouped=true"):
            response = await client.get(
                f"/api/v1/channels/{sample_channel.id}/resources{suffix}"
            )
            assert response.status_code == 200
            payload = response.json()["data"]
            items = payload["groups"] if isinstance(payload, dict) else payload
            resources = [
                resource
                for item in items
                for resource in (item.get("resources", []) if isinstance(item, dict) else [])
            ] if suffix else items
            by_id = {resource["id"]: resource for resource in resources}
            assert by_id[with_task]["has_download_task"] is True
            assert by_id[without_task]["has_download_task"] is False

    async def test_get_resource(self, client, sample_channel, db_session_factory):
        rid = await _make_resource(db_session_factory, sample_channel.id, title_raw="Rget")
        res = await client.get(f"/api/v1/resources/{rid}")
        assert res.status_code == 200
        assert res.json()["data"]["id"] == rid

    async def test_list_includes_work_collection(
        self, client, sample_channel, db_session_factory
    ):
        """The work-collection brief rides along on the nested series/movie
        (eager-loaded); serialization must not recurse on the
        collection.members backref."""
        from app.models.series import TVSeries
        from app.models.work_collection import WorkCollection

        async with db_session_factory() as s:
            coll = WorkCollection(
                title_cn="测试合集", external_source="tmdb_collection", external_id="1"
            )
            series = TVSeries(
                title_cn="剧集", external_id="tmdb:1", external_source="tmdb",
                collection=coll,
            )
            s.add_all([coll, series])
            await s.flush()
            sid = series.id
            await s.commit()
        await _make_resource(
            db_session_factory, sample_channel.id, title_raw="R-coll", series_id=sid
        )
        res = await client.get(
            f"/api/v1/channels/{sample_channel.id}/resources?matched=true"
        )
        assert res.status_code == 200, res.text[:500]
        data = res.json()["data"]
        items = data["groups"] if isinstance(data, dict) else data
        item = items[0]
        if "resources" in item:
            item = item["resources"][0]
        assert item["series"]["collection"]["title_cn"] == "测试合集"

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


@pytest.mark.skip(reason="resource-scoped metadata endpoints were replaced by the unified metadata/associations APIs")
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

    async def test_search_result_none_genre_coerced(self, client, sample_channel, db_session_factory):
        """LLM 候选 genre=None（未提供）不再 500，响应统一为空列表。"""
        rid = await _make_resource(db_session_factory, sample_channel.id, title_raw="RAW-g")
        fake = [{"content_type": "tv", "title_cn": "猫与龙", "genre": None}]
        with patch(
            "app.services.metadata_service.search_metadata_via_llm",
            new_callable=AsyncMock, return_value=fake,
        ):
            res = await client.post(f"/api/v1/resources/{rid}/metadata/search",
                                    json={"search_title": "猫与龙", "content_type": "tv"})
        assert res.status_code == 200
        assert res.json()["data"]["results"][0]["genre"] == []

    async def test_search_forwards_source_and_fallback_sites(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id, title_raw="RAW-source",
        )
        with patch(
            "app.services.metadata_service.search_metadata_via_llm",
            new_callable=AsyncMock,
            return_value=[],
        ) as search:
            res = await client.post(
                f"/api/v1/resources/{rid}/metadata/search",
                json={
                    "search_title": "Someya-san",
                    "content_type": "tv",
                    "data_source_type": "bangumi",
                    "fallback_sources": ["bangumi", "anilist"],
                },
            )
        assert res.status_code == 200
        search.assert_awaited_once_with(
            "Someya-san", "bangumi", ["bangumi", "anilist"]
        )

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

    async def test_link_movie_clears_ambiguous(
        self, client, sample_channel, db_session_factory
    ):
        """Relinking an ambiguous resource to a movie settles the episode/
        season question — a movie carries no episode number."""
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            season=None, episode=5, episode_confidence="ambiguous",
        )
        sel = {
            "content_type": "movie",
            "title_cn": "电影", "title_en": "Film", "original_title": "Film",
            "external_id": "ext-movie", "external_source": "manual",
        }
        with patch(
            "app.services.metadata_service.download_and_cache_poster",
            new_callable=AsyncMock, return_value=None,
        ):
            res = await client.put(f"/api/v1/resources/{rid}/metadata/link",
                                   json={"selected_result": sel})
        assert res.status_code == 200
        d = res.json()["data"]
        assert d["movie_id"] is not None
        assert d["series_id"] is None
        assert d["episode_confidence"] is None


class TestEpisodeCorrection:
    """PATCH /resources/{id}/episode — user manually confirms the per-season
    episode number for ambiguous / reconciled resources."""

    async def test_marks_manual_and_preserves_absolute(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            episode=200, absolute_episode=200, episode_confidence="ambiguous",
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}/episode",
            json={"episode": 13, "absolute_episode": 85},
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["episode"] == 13
        assert body["absolute_episode"] == 85
        assert body["episode_confidence"] == "manual"

    async def test_preserves_prior_absolute_when_omitted(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            episode=200, absolute_episode=200, episode_confidence="ambiguous",
        )
        # User only wants to change the per-season episode; leave absolute alone.
        res = await client.patch(
            f"/api/v1/resources/{rid}/episode",
            json={"episode": 13},
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["episode"] == 13
        assert body["absolute_episode"] == 200  # untouched
        assert body["episode_confidence"] == "manual"

    async def test_updates_season_when_provided(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            season=1, episode=3, episode_confidence="ambiguous",
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}/episode",
            json={"episode": 5, "season": 2},
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["episode"] == 5
        assert body["season"] == 2
        assert body["episode_confidence"] == "manual"

    async def test_preserves_prior_season_when_omitted(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            season=3, episode=10, episode_confidence="ambiguous",
        )
        # User only corrects the episode; season should be left untouched.
        res = await client.patch(
            f"/api/v1/resources/{rid}/episode",
            json={"episode": 11},
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["episode"] == 11
        assert body["season"] == 3  # untouched
        assert body["episode_confidence"] == "manual"

    async def test_404(self, client):
        res = await client.patch(
            "/api/v1/resources/nope/episode",
            json={"episode": 5},
        )
        assert res.status_code == 404

    async def _make_series_with_seasons(self, db_session_factory):
        from app.models.series import TVSeries
        sid = _uuid()
        async with db_session_factory() as s:
            s.add(TVSeries(
                id=sid, title_cn="剧", title_en="Show", content_type="tv",
                number_of_seasons=4,
                seasons=[{"season_number": n, "episode_count": 24} for n in (1, 2, 3, 4)],
            ))
            await s.commit()
        return sid

    async def test_derives_season_from_absolute_when_omitted(
        self, client, sample_channel, db_session_factory,
    ):
        """absolute 89 + known per-season counts pins down S4E17 — the caller
        only confirmed the episode, so the season is derived."""
        sid = await self._make_series_with_seasons(db_session_factory)
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            series_id=sid, season=None, episode=89, absolute_episode=89,
            episode_confidence="ambiguous",
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}/episode",
            json={"episode": 17},
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["season"] == 4
        assert body["episode"] == 17
        assert body["episode_confidence"] == "manual"

    async def test_explicit_season_wins_over_derivation(
        self, client, sample_channel, db_session_factory,
    ):
        sid = await self._make_series_with_seasons(db_session_factory)
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            series_id=sid, season=None, episode=89, absolute_episode=89,
            episode_confidence="ambiguous",
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}/episode",
            json={"episode": 17, "season": 2},
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["season"] == 2  # explicit value wins; no derivation

    async def test_no_seasons_data_no_derivation(
        self, client, sample_channel, db_session_factory,
    ):
        """Series without per-season counts → nothing to derive from."""
        from app.models.series import TVSeries
        sid = _uuid()
        async with db_session_factory() as s:
            s.add(TVSeries(id=sid, title_cn="剧", title_en="Show", content_type="tv"))
            await s.commit()
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            series_id=sid, season=None, episode=89, absolute_episode=89,
            episode_confidence="ambiguous",
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}/episode",
            json={"episode": 17},
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["season"] is None
        assert body["episode"] == 17
        assert body["episode_confidence"] == "manual"


class TestResourceMatchedFilter:
    """The channel page splits parsed/unparsed into tabs via the `matched` param."""

    async def test_matched_filter_splits_parsed_and_unparsed(self, client, sample_channel, db_session_factory):
        from app.models.series import TVSeries
        # Two unmatched + one matched (linked to a series)
        await _make_resource(db_session_factory, sample_channel.id, title_raw="U1")
        await _make_resource(db_session_factory, sample_channel.id, title_raw="U2")
        sid = _uuid()
        async with db_session_factory() as s:
            s.add(TVSeries(id=sid, title_cn="剧", title_en="Show", content_type="tv"))
            await s.commit()
        await _make_resource(
            db_session_factory, sample_channel.id,
            title_raw="M1", series_id=sid, metadata_matched_at=datetime.now(UTC),
        )
        base = f"/api/v1/channels/{sample_channel.id}/resources"

        # matched=true -> only the linked resource
        r = (await client.get(f"{base}?matched=true")).json()
        assert r["meta"]["total"] == 1
        assert r["data"][0]["title_raw"] == "M1"

        # matched=false -> only the two unmatched
        r = (await client.get(f"{base}?matched=false")).json()
        assert r["meta"]["total"] == 2
        assert {d["title_raw"] for d in r["data"]} == {"U1", "U2"}

        # default (no matched) -> all three
        r = (await client.get(base)).json()
        assert r["meta"]["total"] == 3

    async def test_matched_true_grouped_excludes_unknown(self, client, sample_channel, db_session_factory):
        from app.models.series import TVSeries
        await _make_resource(db_session_factory, sample_channel.id, title_raw="U1")  # unmatched
        sid = _uuid()
        async with db_session_factory() as s:
            s.add(TVSeries(id=sid, title_cn="剧", title_en="Show", content_type="tv"))
            await s.commit()
        await _make_resource(
            db_session_factory, sample_channel.id,
            title_raw="M1", series_id=sid, metadata_matched_at=datetime.now(UTC),
        )
        res = await client.get(
            f"/api/v1/channels/{sample_channel.id}/resources?grouped=true&matched=true"
        )
        groups = res.json()["data"]["groups"]
        types = {g["type"] for g in groups}
        assert types == {"series"}  # no "unknown" bucket when matched=true

    async def test_matched_false_paginates(self, client, sample_channel, db_session_factory):
        for i in range(3):
            await _make_resource(db_session_factory, sample_channel.id, title_raw=f"U{i}")
        base = f"/api/v1/channels/{sample_channel.id}/resources?matched=false"
        p1 = (await client.get(f"{base}&page=1&page_size=2")).json()
        assert p1["meta"]["total"] == 3
        assert len(p1["data"]) == 2
        p2 = (await client.get(f"{base}&page=2&page_size=2")).json()
        assert len(p2["data"]) == 1


# ---------------------------------------------------------------------------
# GET /resources/{id}/files
# ---------------------------------------------------------------------------

def _write_torrent(tmp_path, entries):
    """Build a multi-file .torrent from (path components, length) pairs."""
    import bencodepy

    p = tmp_path / "r.torrent"
    p.write_bytes(bencodepy.encode({
        b"info": {
            b"name": b"root",
            b"files": [
                {b"length": length, b"path": [c.encode() for c in parts]}
                for parts, length in entries
            ],
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
    }))
    return str(p)


class TestResourceFiles:
    async def test_404(self, client):
        res = await client.get("/api/v1/resources/nope/files")
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "NOT_FOUND"

    async def test_torrent_cache_hit(
        self, client, sample_channel, db_session_factory, tmp_path,
    ):
        path = _write_torrent(tmp_path, [(["a.mkv"], 100), (["sub", "b.mkv"], 200)])
        rid = await _make_resource(
            db_session_factory, sample_channel.id, torrent_file=path,
        )
        res = await client.get(f"/api/v1/resources/{rid}/files")
        assert res.status_code == 200
        d = res.json()["data"]
        assert d["source"] == "torrent_cache"
        assert d["files"] == [
            {"name": "a.mkv", "size": 100},
            {"name": "sub/b.mkv", "size": 200},
        ]

    async def test_torrent_fetch_live(
        self, client, sample_channel, db_session_factory, tmp_path,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            torrent_url="https://x/r.torrent",
        )
        path = _write_torrent(tmp_path, [(["c.mkv"], 300)])
        with patch(
            "app.api.v1.resources.fetch_torrent_file",
            new_callable=AsyncMock, return_value=path,
        ):
            res = await client.get(f"/api/v1/resources/{rid}/files")
        assert res.status_code == 200
        d = res.json()["data"]
        assert d["source"] == "torrent_fetch"
        assert d["files"] == [{"name": "c.mkv", "size": 300}]
        # The freshly cached path is persisted back onto the resource.
        from app.models.file_resource import FileResource
        async with db_session_factory() as s:
            r = await s.get(FileResource, rid)
            assert r.torrent_file == path

    async def test_downloader_fallback(
        self, client, sample_channel, sample_downloader, db_session_factory, monkeypatch,
    ):
        from types import SimpleNamespace

        from app.models.download_task import DownloadTask
        rid = await _make_resource(db_session_factory, sample_channel.id)  # magnet URL
        async with db_session_factory() as s:
            s.add(DownloadTask(
                id=_uuid(), file_resource_id=rid,
                downloader_id=sample_downloader.id, download_dir="/d",
                transmission_torrent_id=42, status="completed",
            ))
            await s.commit()
        fake_client = SimpleNamespace(
            get_torrent_files=AsyncMock(return_value={
                "name": "t",
                "files": [{"name": "root/x.mkv", "size": 5}],
            }),
        )
        monkeypatch.setattr(
            "app.clients.downloader.get_downloader_client", lambda d: fake_client,
        )
        res = await client.get(f"/api/v1/resources/{rid}/files")
        assert res.status_code == 200
        d = res.json()["data"]
        assert d["source"] == "downloader"
        assert d["files"] == [{"name": "root/x.mkv", "size": 5}]

    async def test_downloader_rpc_failure_falls_through(
        self, client, sample_channel, sample_downloader, db_session_factory, monkeypatch,
    ):
        from types import SimpleNamespace

        from app.models.download_task import DownloadTask
        rid = await _make_resource(db_session_factory, sample_channel.id)
        async with db_session_factory() as s:
            s.add(DownloadTask(
                id=_uuid(), file_resource_id=rid,
                downloader_id=sample_downloader.id, download_dir="/d",
                transmission_torrent_id=42, status="completed",
            ))
            await s.commit()
        fake_client = SimpleNamespace(
            get_torrent_files=AsyncMock(side_effect=RuntimeError("rpc down")),
        )
        monkeypatch.setattr(
            "app.clients.downloader.get_downloader_client", lambda d: fake_client,
        )
        res = await client.get(f"/api/v1/resources/{rid}/files")
        assert res.status_code == 200
        assert res.json()["data"] == {"files": [], "source": "none"}

    async def test_notification_snapshot_fallback(
        self, client, sample_channel, sample_downloader, db_session_factory,
    ):
        from app.models.download_notification import DownloadNotification
        from app.models.download_task import DownloadTask
        rid = await _make_resource(db_session_factory, sample_channel.id)
        task_id = _uuid()
        async with db_session_factory() as s:
            s.add(DownloadTask(
                id=task_id, file_resource_id=rid,
                downloader_id=sample_downloader.id, download_dir="/d",
                transmission_torrent_id=None, status="completed",
            ))
            s.add(DownloadNotification(
                id=_uuid(), agent_id=None, download_task_id=task_id,
                payload={"files": [{"name": "n.mkv", "size": 9}]},
            ))
            await s.commit()
        res = await client.get(f"/api/v1/resources/{rid}/files")
        assert res.status_code == 200
        d = res.json()["data"]
        assert d["source"] == "notification"
        assert d["files"] == [{"name": "n.mkv", "size": 9}]

    async def test_none_when_no_source(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(db_session_factory, sample_channel.id)  # magnet, no task
        res = await client.get(f"/api/v1/resources/{rid}/files")
        assert res.status_code == 200
        assert res.json()["data"] == {"files": [], "source": "none"}


class TestParseCorrection:
    """PATCH /resources/{id} — manual correction of parsed fields."""

    async def test_single_to_batch_clears_episode_and_defaults_scope(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            episode=5, season=1, is_batch=False,
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}",
            json={"is_batch": True, "episode_start": 1, "episode_end": 12},
        )
        assert res.status_code == 200
        d = res.json()["data"]
        assert d["is_batch"] is True
        assert d["episode"] is None
        assert d["batch_scope"] == "season"
        assert d["episode_start"] == 1
        assert d["episode_end"] == 12

    async def test_batch_to_single_clears_scope_and_range(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            is_batch=True, batch_scope="season",
            episode=None, episode_start=1, episode_end=12,
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}",
            json={"is_batch": False, "episode": 7},
        )
        assert res.status_code == 200
        d = res.json()["data"]
        assert d["is_batch"] is False
        assert d["batch_scope"] is None
        assert d["episode_start"] is None
        assert d["episode_end"] is None
        assert d["episode"] == 7
        assert d["episode_confidence"] == "manual"

    async def test_explicit_episode_marks_manual(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            episode=200, episode_confidence="ambiguous",
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}", json={"episode": 13},
        )
        assert res.status_code == 200
        d = res.json()["data"]
        assert d["episode"] == 13
        assert d["episode_confidence"] == "manual"

    async def test_non_episode_fields_do_not_mark_manual(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id, is_batch=False,
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}", json={"is_batch": True},
        )
        assert res.status_code == 200
        d = res.json()["data"]
        assert d["is_batch"] is True
        assert d["batch_scope"] == "season"
        assert d["episode_confidence"] is None

    async def test_batch_mark_clears_ambiguous(
        self, client, sample_channel, db_session_factory,
    ):
        """Marking an ambiguous resource as 合集 settles the episode/season
        question even when no episode field is sent — a batch bypasses
        per-episode flow, so the ambiguous flag would otherwise pin it on
        the dashboard 待确认 list forever."""
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            season=None, episode=None, episode_confidence="ambiguous",
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}", json={"is_batch": True},
        )
        assert res.status_code == 200
        d = res.json()["data"]
        assert d["is_batch"] is True
        assert d["episode_confidence"] == "manual"

    async def test_explicit_batch_scope_wins_over_default(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.patch(
            f"/api/v1/resources/{rid}",
            json={"is_batch": True, "batch_scope": "multi_season"},
        )
        assert res.status_code == 200
        assert res.json()["data"]["batch_scope"] == "multi_season"

    async def test_invalid_batch_scope_422(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.patch(
            f"/api/v1/resources/{rid}",
            json={"is_batch": True, "batch_scope": "bogus"},
        )
        assert res.status_code == 422

    async def test_enqueues_targeted_agent_run(
        self, client, sample_channel, sample_downloader, db_session_factory, monkeypatch,
    ):
        from types import SimpleNamespace

        from app.models.agent import Agent
        agent_id = _uuid()
        async with db_session_factory() as s:
            s.add(Agent(
                id=agent_id, name="A", channel_id=sample_channel.id,
                downloader_id=sample_downloader.id,
                scope_channel_wide=True, status="active",
            ))
            await s.commit()
        rid = await _make_resource(db_session_factory, sample_channel.id)
        enqueue = AsyncMock(return_value={"job_id": "j", "status": "queued"})
        monkeypatch.setattr(
            "app.api.v1.resources.task_queue", SimpleNamespace(enqueue=enqueue),
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}", json={"episode": 3},
        )
        assert res.status_code == 200
        enqueue.assert_awaited_once_with(
            "run_agent", f"agent:{agent_id}",
            {"agent_id": agent_id, "resource_ids": [rid]},
        )

    async def test_404(self, client):
        res = await client.patch("/api/v1/resources/nope", json={"episode": 5})
        assert res.status_code == 404


class TestTorrentRecacheOnConfirmation:
    """Manual-confirmation endpoints re-cache a missing .torrent before the
    targeted rerun (best-effort; failures never block the response)."""

    async def test_recaches_when_torrent_file_missing(
        self, client, sample_channel, db_session_factory, monkeypatch,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            torrent_url="https://x/pack.torrent", torrent_file=None,
        )
        ensure = AsyncMock(return_value=None)
        inspect = AsyncMock(return_value=False)
        monkeypatch.setattr("app.api.v1.resources.ensure_torrent_cached", ensure)
        monkeypatch.setattr("app.api.v1.resources.maybe_inspect_torrent", inspect)
        res = await client.patch(
            f"/api/v1/resources/{rid}", json={"episode": 3},
        )
        assert res.status_code == 200
        ensure.assert_awaited_once()
        inspect.assert_awaited_once()

    async def test_skips_when_cache_exists(
        self, client, sample_channel, db_session_factory, monkeypatch, tmp_path,
    ):
        cached = tmp_path / "cached.torrent"
        cached.write_bytes(b"d8:announce0:e")
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            torrent_url="https://x/pack.torrent", torrent_file=str(cached),
        )
        ensure = AsyncMock(return_value=None)
        inspect = AsyncMock(return_value=False)
        monkeypatch.setattr("app.api.v1.resources.ensure_torrent_cached", ensure)
        monkeypatch.setattr("app.api.v1.resources.maybe_inspect_torrent", inspect)
        res = await client.patch(
            f"/api/v1/resources/{rid}", json={"episode": 3},
        )
        assert res.status_code == 200
        ensure.assert_not_awaited()
        inspect.assert_not_awaited()

    async def test_recache_failure_does_not_block_response(
        self, client, sample_channel, db_session_factory, monkeypatch,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            torrent_url="https://x/pack.torrent", torrent_file=None,
        )
        ensure = AsyncMock(side_effect=RuntimeError("network down"))
        monkeypatch.setattr("app.api.v1.resources.ensure_torrent_cached", ensure)
        res = await client.patch(
            f"/api/v1/resources/{rid}", json={"episode": 3},
        )
        assert res.status_code == 200
        assert res.json()["data"]["episode"] == 3
        ensure.assert_awaited_once()

    async def test_skips_magnet_url(
        self, client, sample_channel, db_session_factory, monkeypatch,
    ):
        rid = await _make_resource(db_session_factory, sample_channel.id)  # magnet
        ensure = AsyncMock(return_value=None)
        inspect = AsyncMock(return_value=False)
        monkeypatch.setattr("app.api.v1.resources.ensure_torrent_cached", ensure)
        monkeypatch.setattr("app.api.v1.resources.maybe_inspect_torrent", inspect)
        res = await client.patch(
            f"/api/v1/resources/{rid}", json={"episode": 3},
        )
        assert res.status_code == 200
        ensure.assert_not_awaited()
        inspect.assert_not_awaited()


class TestParseCorrectionGenericFields:
    """PATCH /resources/{id} also accepts generic media-descriptor fields
    (wizard step 3); they never touch episode_confidence."""

    async def test_generic_fields_applied(self, client, sample_channel, db_session_factory):
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.patch(
            f"/api/v1/resources/{rid}",
            json={"resolution": "720p", "subtitle_langs": ["zh-CN"], "source": "WEB-DL"},
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["resolution"] == "720p"
        assert body["subtitle_langs"] == ["zh-CN"]
        assert body["source"] == "WEB-DL"

    async def test_generic_fields_do_not_mark_manual(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id, episode_confidence="raw",
        )
        res = await client.patch(
            f"/api/v1/resources/{rid}", json={"resolution": "720p"},
        )
        assert res.status_code == 200
        assert res.json()["data"]["episode_confidence"] == "raw"


class TestResourceAssociations:
    """PUT /resources/{id}/associations — the edit wizard's write path."""

    async def _make_series(self, db_session_factory, title="剧"):
        from app.models.series import TVSeries

        sid = _uuid()
        async with db_session_factory() as s:
            s.add(TVSeries(id=sid, title_cn=title, title_en="Show", content_type="tv"))
            await s.commit()
        return sid

    async def _make_movie(self, db_session_factory, title="影"):
        from app.models.movie import Movie

        mid = _uuid()
        async with db_session_factory() as s:
            s.add(Movie(id=mid, title_cn=title, title_en="Movie", content_type="movie"))
            await s.commit()
        return mid

    async def test_non_batch_single_work_writes_fk_and_clears_enrichment(
        self, client, sample_channel, db_session_factory,
    ):
        from app.models.work_collection import WorkCollection

        sid = await self._make_series(db_session_factory)
        coll_id = _uuid()
        async with db_session_factory() as s:
            s.add(WorkCollection(
                id=coll_id, title_cn="作品集", external_source="franchise_pack",
            ))
            await s.commit()
        rid = await _make_resource(
            db_session_factory, sample_channel.id,
            is_batch=True, batch_scope="franchise", collection_id=coll_id,
        )
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={"is_batch": False, "works": [{"work_type": "series", "work_id": sid}]},
        )
        assert res.status_code == 200, res.text[:500]
        body = res.json()["data"]
        assert body["is_batch"] is False
        assert body["batch_scope"] is None
        assert body["series_id"] == sid
        assert body["collection_id"] is None
        assert body["work_links"] == []
        assert body["file_assignments"] == []

    async def test_non_batch_two_works_rejected(
        self, client, sample_channel, db_session_factory,
    ):
        sid = await self._make_series(db_session_factory)
        mid = await self._make_movie(db_session_factory)
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={"is_batch": False, "works": [
                {"work_type": "series", "work_id": sid},
                {"work_type": "movie", "work_id": mid},
            ]},
        )
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_unknown_work_rejected(self, client, sample_channel, db_session_factory):
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={"is_batch": False, "works": [
                {"work_type": "series", "work_id": "missing"},
            ]},
        )
        assert res.status_code == 422

    async def test_single_tv_season_pack_derives_season_and_mirrors_fk(
        self, client, sample_channel, db_session_factory,
    ):
        sid = await self._make_series(db_session_factory)
        rid = await _make_resource(db_session_factory, sample_channel.id, season=None)
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={
                "is_batch": True,
                "works": [{"work_type": "series", "work_id": sid}],
                "assignments": [
                    {
                        "file_path": f"Show.S01E0{n}.mkv",
                        "work_type": "series", "work_id": sid,
                        "season": 1, "episode_start": n, "episode_end": n,
                    }
                    for n in (1, 2, 3)
                ],
            },
        )
        assert res.status_code == 200, res.text[:500]
        body = res.json()["data"]
        assert body["is_batch"] is True
        assert body["batch_scope"] == "season"
        # Single work mirrors into the legacy FK (dedup coverage key reads it).
        assert body["series_id"] == sid
        assert len(body["work_links"]) == 1
        assert body["work_links"][0]["series_id"] == sid
        assert len(body["file_assignments"]) == 3
        assert all(a["season"] == 1 for a in body["file_assignments"])
        assert body["season_ranges"] == [{"season": 1, "episode_start": 1, "episode_end": 3}]
        assert body["season"] == 1
        assert body["episode_start"] == 1
        assert body["episode_end"] == 3

    async def test_batch_association_repeated_save_is_idempotent(
        self, client, sample_channel, db_session_factory,
    ):
        sid = await self._make_series(db_session_factory)
        rid = await _make_resource(db_session_factory, sample_channel.id)
        payload = {
            "is_batch": True,
            "works": [{"work_type": "series", "work_id": sid}],
            "assignments": [{
                "file_path": "Show.S01E01.mkv",
                "work_type": "series",
                "work_id": sid,
                "season": 1,
                "episode_start": 1,
                "episode_end": 1,
            }],
        }

        first = await client.put(
            f"/api/v1/resources/{rid}/associations", json=payload,
        )
        second = await client.put(
            f"/api/v1/resources/{rid}/associations", json=payload,
        )

        assert first.status_code == 200, first.text[:500]
        assert second.status_code == 200, second.text[:500]
        links = second.json()["data"]["work_links"]
        assert len(links) == 1
        assert links[0]["series_id"] == sid
        assert links[0]["source"] == "manual"

    async def test_association_fields_can_correct_missing_titles(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(
            db_session_factory, sample_channel.id, title_raw="RAW-title",
        )
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={
                "is_batch": False,
                "works": [],
                "assignments": [],
                "fields": {
                    "title_cn": "新 攻壳机动队",
                    "search_title": "THE.GHOST.IN.THE.SHELL",
                },
            },
        )
        assert res.status_code == 200, res.text[:500]
        assert res.json()["data"]["title_cn"] == "新 攻壳机动队"
        assert res.json()["data"]["search_title"] == "THE.GHOST.IN.THE.SHELL"

    async def test_multi_season_evidence_derives_multi_season(
        self, client, sample_channel, db_session_factory,
    ):
        sid = await self._make_series(db_session_factory)
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={
                "is_batch": True,
                "works": [{"work_type": "series", "work_id": sid}],
                "assignments": [
                    {
                        "file_path": "S01E01.mkv",
                        "work_type": "series", "work_id": sid,
                        "season": 1, "episode_start": 1, "episode_end": 1,
                    },
                    {
                        "file_path": "S02E01.mkv",
                        "work_type": "series", "work_id": sid,
                        "season": 2, "episode_start": 1, "episode_end": 1,
                    },
                ],
            },
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["batch_scope"] == "multi_season"
        assert body["batch_seasons"] == [1, 2]
        assert len(body["season_ranges"]) == 2

    async def test_mixed_works_derive_franchise_and_clear_fks(
        self, client, sample_channel, db_session_factory,
    ):
        sid = await self._make_series(db_session_factory)
        mid = await self._make_movie(db_session_factory)
        rid = await _make_resource(
            db_session_factory, sample_channel.id, series_id=sid,
        )
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={
                "is_batch": True,
                "works": [
                    {"work_type": "series", "work_id": sid},
                    {"work_type": "movie", "work_id": mid},
                ],
                "assignments": [
                    {
                        "file_path": "TV/S01E01.mkv",
                        "work_type": "series", "work_id": sid,
                        "season": 1, "episode_start": 1, "episode_end": 1,
                    },
                    {
                        "file_path": "Movie.mkv",
                        "work_type": "movie", "work_id": mid,
                    },
                ],
            },
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["batch_scope"] == "franchise"
        assert body["series_id"] is None
        assert body["movie_id"] is None
        assert {link["source"] for link in body["work_links"]} == {"manual"}

    async def test_all_movies_derive_movies_scope(
        self, client, sample_channel, db_session_factory,
    ):
        m1 = await self._make_movie(db_session_factory, title="影1")
        m2 = await self._make_movie(db_session_factory, title="影2")
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={
                "is_batch": True,
                "works": [
                    {"work_type": "movie", "work_id": m1},
                    {"work_type": "movie", "work_id": m2},
                ],
                "assignments": [
                    {"file_path": "A.mkv", "work_type": "movie", "work_id": m1},
                    {"file_path": "B.mkv", "work_type": "movie", "work_id": m2},
                ],
            },
        )
        assert res.status_code == 200
        assert res.json()["data"]["batch_scope"] == "movies"

    async def test_overlap_rejected_with_422(
        self, client, sample_channel, db_session_factory,
    ):
        sid = await self._make_series(db_session_factory)
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={
                "is_batch": True,
                "works": [{"work_type": "series", "work_id": sid}],
                "assignments": [
                    {
                        "file_path": "A.mkv", "work_type": "series", "work_id": sid,
                        "season": 1, "episode_start": 1, "episode_end": 5,
                    },
                    {
                        "file_path": "B.mkv", "work_type": "series", "work_id": sid,
                        "season": 1, "episode_start": 5, "episode_end": 8,
                    },
                ],
            },
        )
        assert res.status_code == 422
        assert "重叠" in res.json()["error"]["message"]

    async def test_gap_returns_warning_not_error(
        self, client, sample_channel, db_session_factory,
    ):
        sid = await self._make_series(db_session_factory)
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={
                "is_batch": True,
                "works": [{"work_type": "series", "work_id": sid}],
                "assignments": [
                    {
                        "file_path": "A.mkv", "work_type": "series", "work_id": sid,
                        "season": 1, "episode_start": 1, "episode_end": 2,
                    },
                    {
                        "file_path": "B.mkv", "work_type": "series", "work_id": sid,
                        "season": 1, "episode_start": 5, "episode_end": 6,
                    },
                ],
            },
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert any("断档" in w for w in body["warnings"])

    async def test_tv_assignment_without_season_rejected(
        self, client, sample_channel, db_session_factory,
    ):
        sid = await self._make_series(db_session_factory)
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={
                "is_batch": True,
                "works": [{"work_type": "series", "work_id": sid}],
                "assignments": [
                    {"file_path": "A.mkv", "work_type": "series", "work_id": sid},
                ],
            },
        )
        assert res.status_code == 422
        assert "季" in res.json()["error"]["message"]

    async def test_assignment_outside_works_rejected(
        self, client, sample_channel, db_session_factory,
    ):
        sid = await self._make_series(db_session_factory)
        other = await self._make_series(db_session_factory, title="别的")
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={
                "is_batch": True,
                "works": [{"work_type": "series", "work_id": sid}],
                "assignments": [
                    {
                        "file_path": "A.mkv", "work_type": "series", "work_id": other,
                        "season": 1, "episode_start": 1, "episode_end": 1,
                    },
                ],
            },
        )
        assert res.status_code == 422

    async def test_unknown_collection_rejected(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={
                "is_batch": True,
                "collection_id": "missing-coll",
            },
        )
        assert res.status_code == 422

    async def test_fields_applied_in_same_call(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={
                "is_batch": False,
                "fields": {"resolution": "2160p", "container": "mkv"},
            },
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["resolution"] == "2160p"
        assert body["container"] == "mkv"


class TestAnalyzeBatch:
    """POST /resources/{id}/analyze-batch — non-persistent LLM suggestions."""

    async def test_no_listing_returns_null_suggestion(
        self, client, sample_channel, db_session_factory,
    ):
        rid = await _make_resource(db_session_factory, sample_channel.id)
        res = await client.post(f"/api/v1/resources/{rid}/analyze-batch")
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["suggestion"] is None
        assert body["listing_source"] == "none"

    async def test_suggestion_returned_without_persistence(
        self, client, sample_channel, db_session_factory, monkeypatch,
    ):
        rid = await _make_resource(db_session_factory, sample_channel.id)

        async with db_session_factory() as s:
            from app.models.file_resource import FileResource

            row = await s.get(FileResource, rid)
            cache = row.torrent_file  # None — patch the resolver instead
        assert cache is None

        async def fake_resolve(db, resource):
            return (
                [
                    {"name": "作品A TV/作品A S01E01.mkv", "size": 500 * 1024 * 1024},
                    {"name": "作品B 剧场版/电影.mkv", "size": 4 * 1024 * 1024 * 1024},
                ],
                "torrent_cache",
            )

        monkeypatch.setattr(
            "app.api.v1.resources._resolve_resource_files", fake_resolve,
        )
        monkeypatch.setattr(
            "app.services.batch_content_analysis.analyze_listing",
            AsyncMock(return_value={
                "scope": "mixed",
                "works": [
                    {
                        "title": "作品A",
                        "content_type": "tv",
                        "files": [{
                            "path": "作品A TV/作品A S01E01.mkv",
                            "season": 1, "episode_start": 1, "episode_end": 1,
                        }],
                    },
                    {
                        "title": "作品B 剧场版",
                        "content_type": "movie",
                        "files": [{"path": "作品B 剧场版/电影.mkv"}],
                    },
                ],
            }),
        )
        res = await client.post(f"/api/v1/resources/{rid}/analyze-batch")
        assert res.status_code == 200, res.text[:500]
        data = res.json()["data"]
        sug = data["suggestion"]
        assert sug is not None
        # Deterministic layer always present (no LLM needed).
        det = sug["deterministic"]
        assert len(det["files"]) == 2
        assert det["files"][0]["season"] == 1 and det["files"][0]["episode"] == 1
        assert data["listing_source"] == "torrent_cache"
        # LLM works block rides along when the analyzer produced one.
        assert len(sug["works"]) == 2
        # Nothing persisted.
        async with db_session_factory() as s:
            from app.models.resource_file_assignment import ResourceFileAssignment

            rows = (await s.execute(
                __import__("sqlalchemy").select(ResourceFileAssignment)
                .where(ResourceFileAssignment.resource_id == rid)
            )).scalars().all()
        assert rows == []

    async def test_404(self, client):
        res = await client.post("/api/v1/resources/nope/analyze-batch")
        assert res.status_code == 404


class TestAssociationAudioPreservation:
    """A media-fields-only save (works=[]) must not unlink an AudioWork."""

    async def test_empty_works_preserves_audio_link(
        self, client, sample_channel, db_session_factory,
    ):
        from app.models.audio_work import AudioWork

        aid = _uuid()
        async with db_session_factory() as s:
            s.add(AudioWork(id=aid, title_cn="ASMR"))
            await s.commit()
        rid = await _make_resource(
            db_session_factory, sample_channel.id, audio_work_id=aid,
        )
        res = await client.put(
            f"/api/v1/resources/{rid}/associations",
            json={"is_batch": False, "works": [], "fields": {"resolution": "1080p"}},
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["audio_work_id"] == aid
        assert body["resolution"] == "1080p"
