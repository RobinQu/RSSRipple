"""API tests for FileResource endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch


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
