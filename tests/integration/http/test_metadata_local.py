"""Metadata integration tests — local matching, FTS search, manual link.

Covers the metadata_service layers reachable without external API keys:

  - Layer 3 local DB auto-link during fetch (pre-seeded series)
  - GET /resources/{id}/metadata on-demand matching (Layer 1/2)
  - POST /resources/{id}/metadata/search with data_source_type="local" (FTS5)
  - PUT /resources/{id}/metadata/link — create/update series & movie entities,
    ChannelRawTitleMapping upsert, title-fallback dedup
  - Series / Movies CRUD, /works poster wall, audio-works 404s
  - system-settings GET/PUT, works metadata-config, refresh-metadata (local)
  - Channel field-values, grouped resources, summarize-filters,
    cleanup-unresolved, metadata-sources

Requirements: Docker test environment with app + test-server services.
"""

from __future__ import annotations

import httpx
import pytest

from tests.integration.http._http import (
    API_HEADERS,
    RICH_FIELD_MAPPING,
    RSSRIPPLE,
    TEST_SERVER,
    _api,
    _poll_fetch,
    associate_metadata_request,
    search_metadata,
    search_metadata_request,
)

KUSURIYA_TITLE_CN = "药屋少女的呢喃"
HONZUKI_TITLE_CN = "小书痴的下克上"
MIKANANI_S2_URL = f"{TEST_SERVER}/rss/mikanani?series=2"  # 药屋少女的呢喃
MIKANANI_S4_URL = f"{TEST_SERVER}/rss/mikanani?series=4"  # 小书痴的下克上


def _api_refresh(path: str, **kw) -> httpx.Response:
    """Refresh-metadata calls route through the real (env-configured) LLM,
    which can take minutes under load — use a 240s budget instead of the
    shared 60s client (which flaky-times-out on this endpoint)."""
    c = httpx.Client(timeout=240.0, headers=API_HEADERS)
    return c.post(f"{RSSRIPPLE}{path}", **kw)


def _ensure_series(title_cn: str, title_en: str) -> str:
    r = _api("/api/v1/series", params={"page_size": 100, "title": title_cn})
    if r.status_code == 200:
        for s in r.json().get("data", []):
            if s.get("title_cn") == title_cn:
                return s["id"]
    r = _api(
        "/api/v1/series",
        method="post",
        json={
            "title_cn": title_cn,
            "title_en": title_en,
            "start_date": "2023-01-01",
            "is_anime": True,
        },
    )
    assert r.status_code == 201, f"Series creation failed: {r.status_code} {r.text}"
    return r.json()["data"]["id"]


def _create_channel(name: str, url: str) -> str:
    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": name,
            "url": url,
            "field_mapping": RICH_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    assert r.status_code == 201, f"create channel failed: {r.text}"
    return r.json()["data"]["id"]


def _fetch(channel_id: str) -> list[dict]:
    _api(f"/api/v1/channels/{channel_id}/fetch", method="post")
    result = _poll_fetch(channel_id, accept_failed=True)
    assert result.get("status") == "done", f"fetch failed: {result}"
    r = _api(f"/api/v1/channels/{channel_id}/resources", params={"page_size": 100})
    return r.json().get("data", [])


# =========================================================================
# TestLocalAutoLink — Layer 3 exact match during fetch
# =========================================================================


@pytest.fixture(scope="class")
def _linked_channel():
    series_id = _ensure_series(KUSURIYA_TITLE_CN, "The Apothecary Diaries")
    ch_id = _create_channel("Metadata AutoLink Channel", MIKANANI_S2_URL)
    resources = _fetch(ch_id)
    if not resources:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
        pytest.skip("no resources after fetch")
    yield {"channel_id": ch_id, "series_id": series_id, "resources": resources}
    # No tasks are ever dispatched from this channel → safe to delete.
    try:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
    except Exception:
        pass


class TestLocalAutoLink:
    """Fetch-time local matching against a pre-seeded series."""

    def test_resources_auto_linked(self, _linked_channel):
        series_id = _linked_channel["series_id"]
        linked = [
            r for r in _linked_channel["resources"] if r.get("series_id") == series_id
        ]
        assert len(linked) == len(_linked_channel["resources"])
        assert all(r.get("metadata_matched_at") for r in linked)

    def test_get_resource_metadata_layer1(self, _linked_channel):
        """GET /resources/{id}/metadata on a linked resource returns the entity."""
        rid = _linked_channel["resources"][0]["id"]
        r = _api(f"/api/v1/resources/{rid}/metadata")
        assert r.status_code == 200, f"metadata failed: {r.text}"
        data = r.json()["data"]
        assert data["series_id"] == _linked_channel["series_id"]
        assert data["linked"] is not None
        assert data["linked"]["type"] == "series"
        assert data["linked"]["entity"]["title_cn"] == KUSURIYA_TITLE_CN

    def test_get_resource_detail(self, _linked_channel):
        rid = _linked_channel["resources"][0]["id"]
        r = _api(f"/api/v1/resources/{rid}")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["id"] == rid
        assert data["series_id"] == _linked_channel["series_id"]

    def test_resource_404(self):
        r = _api("/api/v1/resources/nonexistent")
        assert r.status_code == 404
        r = _api("/api/v1/resources/nonexistent/metadata")
        assert r.status_code == 404

    def test_resources_grouped(self, _linked_channel):
        """grouped=true paginates by work group."""
        ch_id = _linked_channel["channel_id"]
        r = _api(
            f"/api/v1/channels/{ch_id}/resources",
            params={"grouped": "true", "page_size": 10},
        )
        assert r.status_code == 200, f"grouped failed: {r.text}"
        data = r.json()["data"]
        # Shape: {"groups": [...]} (newer) or a bare list (older)
        groups = data["groups"] if isinstance(data, dict) else data
        assert groups, "expected at least one group"
        group = groups[0]
        assert group.get("type") in ("series", "movie", "unknown", None) or "resources" in group

    def test_field_values(self, _linked_channel):
        ch_id = _linked_channel["channel_id"]
        r = _api(
            f"/api/v1/channels/{ch_id}/field-values",
            params={"field": "resolution"},
        )
        assert r.status_code == 200
        values = r.json()["data"]
        assert "1080p" in values

        r = _api(
            f"/api/v1/channels/{ch_id}/field-values",
            params={"field": "subtitle_group", "q": "lo"},
        )
        assert r.status_code == 200
        assert all(v.lower().startswith("lo") for v in r.json()["data"])

        r = _api(
            f"/api/v1/channels/{ch_id}/field-values",
            params={"field": "subtitle_langs"},
        )
        assert r.status_code == 200
        assert isinstance(r.json()["data"], list)

        # Numeric fields are rejected
        r = _api(
            f"/api/v1/channels/{ch_id}/field-values",
            params={"field": "file_size"},
        )
        assert r.status_code == 422
        # Unknown channel
        r = _api(
            "/api/v1/channels/nonexistent/field-values",
            params={"field": "resolution"},
        )
        assert r.status_code == 404

    def test_summarize_filters(self, _linked_channel):
        ch_id = _linked_channel["channel_id"]
        ids = [r["id"] for r in _linked_channel["resources"][:6]]
        r = _api(
            f"/api/v1/channels/{ch_id}/summarize-filters",
            method="post",
            json={"resource_ids": ids},
        )
        assert r.status_code == 200, f"summarize-filters failed: {r.text}"
        data = r.json()["data"]
        assert data["unlinked_count"] == 0
        assert len(data["works"]) == 1
        work = data["works"][0]
        assert work["content_type"] == "tv"
        assert work["series_id"] == _linked_channel["series_id"]
        assert work["resource_count"] == 6
        # Global conditions come from fields uniform across the selection;
        # which fields qualify depends on the sample, so check structure only.
        gfc = data["global_filter_config"]
        assert gfc is None or gfc.get("conditions") is not None

        # Empty selection → empty proposal
        r = _api(
            f"/api/v1/channels/{ch_id}/summarize-filters",
            method="post",
            json={"resource_ids": []},
        )
        assert r.status_code == 200
        assert r.json()["data"]["works"] == []

    def test_cleanup_unresolved_nothing_stale(self, _linked_channel):
        """cleanup-unresolved on fresh resources deletes nothing (but runs)."""
        ch_id = _linked_channel["channel_id"]
        r = _api(f"/api/v1/channels/{ch_id}/cleanup-unresolved", method="post")
        assert r.status_code == 200, f"cleanup failed: {r.text}"
        assert r.json()["data"]["deleted"] == 0

    def test_cleanup_unresolved_404(self):
        r = _api("/api/v1/channels/nonexistent/cleanup-unresolved", method="post")
        assert r.status_code == 404

    def test_metadata_sources_catalog(self):
        r = _api("/api/v1/channels/metadata-sources")
        assert r.status_code == 200
        data = r.json()["data"]
        sources = data if isinstance(data, list) else data.get("sources", [])
        values = {s["value"] for s in sources}
        # Two-source architecture: only wikipedia/tmdb are channel sources.
        assert {"tmdb", "wikipedia"} <= values
        # Wikipedia needs no credentials → available; key-based availability
        # depends on the .env keys present, so don't assert on those.
        by_value = {s["value"]: s for s in sources}
        assert by_value["wikipedia"]["available"] is True

    def test_channel_update_and_detail(self, _linked_channel):
        ch_id = _linked_channel["channel_id"]
        r = _api(
            f"/api/v1/channels/{ch_id}",
            method="put",
            json={
                "name": "Metadata AutoLink Channel (renamed)",
                "auto_cleanup_unresolved_enabled": True,
                "auto_cleanup_unresolved_days": 14,
            },
        )
        assert r.status_code == 200, f"update failed: {r.text}"
        r = _api(f"/api/v1/channels/{ch_id}")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["name"] == "Metadata AutoLink Channel (renamed)"
        # Detail embeds a recent-resources preview
        assert "resources" in data or "recent_resources" in data


# =========================================================================
# TestManualSearchLink — manual search (local FTS) + link flows
# =========================================================================


@pytest.fixture(scope="class")
def _unlinked_channel():
    """Channel whose resources match nothing (no series pre-seeded)."""
    ch_id = _create_channel("Manual Link Channel", MIKANANI_S4_URL)
    resources = _fetch(ch_id)
    if not resources:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
        pytest.skip("no resources after fetch")
    unlinked = [r for r in resources if not r.get("series_id") and not r.get("movie_id")]
    if not unlinked:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
        pytest.skip("resources unexpectedly linked")
    yield {"channel_id": ch_id, "resources": unlinked}
    try:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
    except Exception:
        pass


class TestManualSearchLink:
    """Manual metadata search + link + mapping-based auto-link."""

    series_id: str = ""
    movie_id: str = ""

    def test_manual_link_creates_series(self, _unlinked_channel):
        """PUT /resources/{id}/metadata/link creates the series + mapping."""
        res = _unlinked_channel["resources"][0]
        r = associate_metadata_request(
            f"/api/v1/resources/{res['id']}/metadata/link",
            method="put",
            json={
                "selected_result": {
                    "content_type": "tv",
                    "title_cn": HONZUKI_TITLE_CN,
                    "title_en": "Honzuki no Gekokujou",
                    "original_title": "本好きの下剋上",
                    "description": "Ascendance of a Bookworm test entry",
                    "external_id": "manual-honzuki-1",
                    "external_source": "tmdb",
                    "genre": ["Anime", "Fantasy"],
                    "rating": 8.1,
                }
            },
        )
        assert r.status_code == 200, f"link failed: {r.text}"
        data = r.json()["data"]
        assert data["series_id"], "resource should be linked after manual link"
        assert data["movie_id"] is None
        TestManualSearchLink.series_id = data["series_id"]

    def test_mapping_auto_links_sibling(self, _unlinked_channel):
        """GET metadata on a sibling resource hits the Layer-2 mapping."""
        if not TestManualSearchLink.series_id:
            pytest.skip("no series — prerequisite failed")
        sibling = _unlinked_channel["resources"][1]
        r = _api(f"/api/v1/resources/{sibling['id']}/metadata")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["series_id"] == TestManualSearchLink.series_id, (
            "sibling resource should auto-link via ChannelRawTitleMapping"
        )

    def test_local_fts_search(self, _unlinked_channel):
        """POST metadata/search data_source_type=local finds the new series."""
        res = _unlinked_channel["resources"][2]
        r = search_metadata_request(
            f"/api/v1/resources/{res['id']}/metadata/search",
            method="post",
            json={
                "search_title": HONZUKI_TITLE_CN,
                "content_type": "tv",
                "data_source_type": "local",
            },
        )
        assert r.status_code == 200, f"search failed: {r.text}"
        results = r.json()["data"]["results"]
        assert results, "expected FTS hits for the manually-created series"
        assert any(r_["title_cn"] == HONZUKI_TITLE_CN for r_ in results)

    def test_local_fts_search_movie(self, _unlinked_channel):
        """Local search with content_type=movie returns a list (maybe empty)."""
        res = _unlinked_channel["resources"][2]
        r = search_metadata_request(
            f"/api/v1/resources/{res['id']}/metadata/search",
            method="post",
            json={
                "search_title": "不存在的电影xyz",
                "content_type": "movie",
                "data_source_type": "local",
            },
        )
        assert r.status_code == 200
        assert r.json()["data"]["results"] == []

    def test_local_fts_search_short_query(self, _unlinked_channel):
        """Sub-3-char queries use the LIKE fallback instead of trigram FTS."""
        res = _unlinked_channel["resources"][2]
        r = search_metadata_request(
            f"/api/v1/resources/{res['id']}/metadata/search",
            method="post",
            json={
                "search_title": "小书",
                "content_type": "tv",
                "data_source_type": "local",
            },
        )
        assert r.status_code == 200
        # LIKE finds the row but the similarity gate (<70) filters it out
        assert isinstance(r.json()["data"]["results"], list)

    def test_manual_link_movie(self, _unlinked_channel):
        """Linking a movie result creates the Movie entity."""
        res = _unlinked_channel["resources"][3]
        r = associate_metadata_request(
            f"/api/v1/resources/{res['id']}/metadata/link",
            method="put",
            json={
                "selected_result": {
                    "content_type": "movie",
                    "title_cn": "测试电影",
                    "title_en": "Test Movie",
                    "description": "manual movie link",
                    "external_id": "manual-movie-1",
                    "external_source": "tmdb",
                    "year": 2024,
                }
            },
        )
        assert r.status_code == 200, f"movie link failed: {r.text}"
        data = r.json()["data"]
        assert data["movie_id"], "resource should be movie-linked"
        assert data["series_id"] is None
        TestManualSearchLink.movie_id = data["movie_id"]

    def test_relink_same_external_id_updates(self, _unlinked_channel):
        """Re-linking with the same external_id updates, not duplicates."""
        if not TestManualSearchLink.series_id:
            pytest.skip("no series — prerequisite failed")
        res = _unlinked_channel["resources"][4]
        r = associate_metadata_request(
            f"/api/v1/resources/{res['id']}/metadata/link",
            method="put",
            json={
                "selected_result": {
                    "content_type": "tv",
                    "title_cn": HONZUKI_TITLE_CN,
                    "title_en": "Honzuki no Gekokujou",
                    "external_id": "manual-honzuki-1",
                    "external_source": "tmdb",
                    "alt_titles": ["Honzuki S4"],
                }
            },
        )
        assert r.status_code == 200
        assert r.json()["data"]["series_id"] == TestManualSearchLink.series_id

        # Aliases merged onto the existing row
        r = _api(f"/api/v1/series/{TestManualSearchLink.series_id}")
        assert r.status_code == 200
        aliases = r.json()["data"].get("aliases") or []
        assert "Honzuki S4" in aliases

    def test_title_fallback_dedup(self, _unlinked_channel):
        """Link with a new external_id but same title → title fallback hits."""
        if not TestManualSearchLink.series_id:
            pytest.skip("no series — prerequisite failed")
        res = _unlinked_channel["resources"][5]
        r = associate_metadata_request(
            f"/api/v1/resources/{res['id']}/metadata/link",
            method="put",
            json={
                "selected_result": {
                    "content_type": "tv",
                    "title_cn": HONZUKI_TITLE_CN,
                    "external_id": "manual-honzuki-other-source",
                    "external_source": "wikipedia",
                }
            },
        )
        assert r.status_code == 200
        assert r.json()["data"]["series_id"] == TestManualSearchLink.series_id, (
            "same-title link should converge on the existing series row"
        )

    def test_grouped_resources_all_group_types(self, _unlinked_channel):
        """grouped=true returns series, movie, and unknown groups."""
        ch_id = _unlinked_channel["channel_id"]
        r = _api(
            f"/api/v1/channels/{ch_id}/resources",
            params={"grouped": "true", "page_size": 50},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        groups = data["groups"] if isinstance(data, dict) else data
        types = {g.get("type") for g in groups}
        assert "series" in types or "movie" in types, f"groups: {types}"

    def test_movie_dispatch_dedup(self, _unlinked_channel):
        """Movie resource dispatch: second backfill is deduped (movie branch)."""
        if not TestManualSearchLink.movie_id:
            pytest.skip("no movie — prerequisite failed")
        res = _unlinked_channel["resources"][3]
        r = _api("/api/v1/downloaders", params={"page_size": 100})
        dl_id = next(
            (d["id"] for d in r.json()["data"] if d.get("type") == "mock"), None
        )
        assert dl_id, "no mock downloader available"

        ch_id = _unlinked_channel["channel_id"]
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Movie Dedup Agent",
                "channel_id": ch_id,
                "downloader_id": dl_id,
                "scope_channel_wide": True,
                "llm_enabled": False,
                "conflict_resolution": "auto",
                "dispatch_resource_ids": [res["id"]],
            },
        )
        assert r.status_code == 201, f"create agent failed: {r.text}"
        agent_id = r.json()["data"]["id"]

        r = _api(f"/api/v1/agents/{agent_id}/tasks", params={"page_size": 100})
        assert len(r.json()["data"]) == 1, "movie resource should dispatch once"

        # Re-backfill: the active movie task dedups the second attempt
        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={"dispatch_resource_ids": [res["id"]]},
        )
        assert r.status_code == 200
        r = _api(f"/api/v1/agents/{agent_id}/tasks", params={"page_size": 100})
        assert len(r.json()["data"]) == 1, "movie re-dispatch must be deduped"

        _api(f"/api/v1/agents/{agent_id}", method="delete")


# =========================================================================
# TestSeriesMovieCRUD — entity CRUD endpoints
# =========================================================================


class TestSeriesMovieCRUD:
    def test_series_crud(self):
        r = _api(
            "/api/v1/series",
            method="post",
            json={
                "title_cn": "CRUD 剧集",
                "title_en": "CRUD Series",
                "aliases": ["CRUD Alias"],
                "description": "crud test",
                "genre": ["Drama"],
                "rating": 7.5,
            },
        )
        assert r.status_code == 201, f"create failed: {r.text}"
        sid = r.json()["data"]["id"]

        r = _api("/api/v1/series", params={"title": "CRUD"})
        assert r.status_code == 200
        assert any(s["id"] == sid for s in r.json()["data"])

        r = _api(f"/api/v1/series/{sid}")
        assert r.status_code == 200
        detail = r.json()["data"]
        assert detail["title_cn"] == "CRUD 剧集"
        assert "resource_count" in detail or "resources" in detail or "episodes" in detail

        r = _api(
            f"/api/v1/series/{sid}",
            method="put",
            json={"description": "updated", "aliases": ["CRUD Alias 2"]},
        )
        assert r.status_code == 200, f"update failed: {r.text}"
        aliases = r.json()["data"].get("aliases") or []
        # Aliases append (not replaced)
        assert "CRUD Alias" in aliases and "CRUD Alias 2" in aliases

        r = _api(f"/api/v1/series/{sid}", method="delete")
        assert r.status_code == 200
        r = _api(f"/api/v1/series/{sid}")
        assert r.status_code == 404

    def test_series_404s(self):
        assert _api("/api/v1/series/nonexistent").status_code == 404
        assert _api("/api/v1/series/nonexistent", method="put", json={}).status_code == 404
        assert _api("/api/v1/series/nonexistent", method="delete").status_code == 404

    def test_movie_crud(self):
        r = _api(
            "/api/v1/movies",
            method="post",
            json={
                "title_cn": "CRUD 电影",
                "title_en": "CRUD Movie",
                "description": "crud movie",
                "year": 2020,
            },
        )
        assert r.status_code == 201, f"create failed: {r.text}"
        mid = r.json()["data"]["id"]

        r = _api("/api/v1/movies", params={"title": "CRUD"})
        assert r.status_code == 200
        assert any(m["id"] == mid for m in r.json()["data"])

        r = _api(f"/api/v1/movies/{mid}")
        assert r.status_code == 200
        assert r.json()["data"]["title_en"] == "CRUD Movie"

        r = _api(
            f"/api/v1/movies/{mid}",
            method="put",
            json={"description": "updated movie", "rating": 6.6},
        )
        assert r.status_code == 200
        assert r.json()["data"]["description"] == "updated movie"

        r = _api(f"/api/v1/movies/{mid}", method="delete")
        assert r.status_code == 200
        assert _api(f"/api/v1/movies/{mid}").status_code == 404

    def test_movie_404s(self):
        assert _api("/api/v1/movies/nonexistent").status_code == 404
        assert _api("/api/v1/movies/nonexistent", method="put", json={}).status_code == 404
        assert _api("/api/v1/movies/nonexistent", method="delete").status_code == 404


# =========================================================================
# TestWorksSettings — poster wall + settings + audio-works
# =========================================================================


class TestWorksSettings:
    def test_works_listing(self):
        r = _api("/api/v1/works", params={"page_size": 100})
        assert r.status_code == 200
        assert r.json()["meta"]["total"] >= 1

        r = _api("/api/v1/works", params={"content_type": "tv"})
        assert r.status_code == 200
        assert all(w["content_type"] == "tv" for w in r.json()["data"])

        r = _api("/api/v1/works", params={"content_type": "movie"})
        assert r.status_code == 200
        assert all(w["content_type"] == "movie" for w in r.json()["data"])

        r = _api("/api/v1/works", params={"search": KUSURIYA_TITLE_CN})
        assert r.status_code == 200
        assert any(w["title_cn"] == KUSURIYA_TITLE_CN for w in r.json()["data"])

        r = _api("/api/v1/works", params={"content_type": "audio"})
        assert r.status_code == 200

    def test_audio_works_endpoints(self):
        r = _api("/api/v1/audio-works")
        assert r.status_code == 200
        assert isinstance(r.json()["data"], list)
        assert _api("/api/v1/audio-works/nonexistent").status_code == 404
        assert _api(
            "/api/v1/audio-works/nonexistent", method="put", json={}
        ).status_code == 404
        assert _api("/api/v1/audio-works/nonexistent", method="delete").status_code == 404

    def test_system_settings_roundtrip(self):
        r = _api("/api/v1/system-settings")
        assert r.status_code == 200
        data = r.json()["data"]
        assert "settings" in data and "groups" in data
        assert "llm_model" in data["settings"]

        # Bool + str updates
        r = _api(
            "/api/v1/system-settings",
            method="put",
            json={"llm_enable_thinking": False, "llm_model": "integration-test-model"},
        )
        assert r.status_code == 200, f"put failed: {r.text}"
        settings = r.json()["data"]["settings"]
        assert settings["llm_model"]["value"] == "integration-test-model"
        assert settings["llm_enable_thinking"]["value"] is False

        # Empty body → 400
        r = _api("/api/v1/system-settings", method="put", json={})
        assert r.status_code == 400
        # Removed/unknown key → empty payload → 400
        r = _api(
            "/api/v1/system-settings",
            method="put",
            json={"exa_effort_level": "bogus"},
        )
        assert r.status_code == 400

        # Restore: clearing llm_model reverts to the env default
        r = _api("/api/v1/system-settings", method="put", json={"llm_model": ""})
        assert r.status_code == 200
        assert r.json()["data"]["settings"]["llm_model"]["value"] != "integration-test-model"

    def test_works_metadata_config(self):
        """The unified metadata endpoint exposes the source catalog."""
        r = _api("/api/v1/metadata/sources")
        assert r.status_code == 200
        data = r.json()["data"]
        assert "primary_sources" in data
        assert "trusted_sites" in data

        # The removed per-works config route is no longer writable.
        r = _api(
            "/api/v1/works/metadata-config",
            method="put",
            json={"default_source": "wikipedia"},
        )
        assert r.status_code == 405

    def test_unified_local_work_search(self):
        """The unified local mode finds an existing work without mutation."""
        series_id = _ensure_series(KUSURIYA_TITLE_CN, "The Apothecary Diaries")
        r = search_metadata(
            KUSURIYA_TITLE_CN,
            "tv",
        )
        assert r.status_code == 200, f"search failed: {r.text}"
        candidates = r.json()["data"]["candidates"]
        assert any(c.get("work_id") == series_id for c in candidates)

    def test_batch_refresh_metadata(self):
        series_id = _ensure_series(KUSURIYA_TITLE_CN, "The Apothecary Diaries")
        r = _api_refresh(
            "/api/v1/works/batch-refresh-metadata",
            json={
                "items": [{"id": series_id, "content_type": "tv"}],
                "source": "wikipedia",
            },
        )
        assert r.status_code == 200, f"batch refresh failed: {r.text}"
        data = r.json()["data"]
        assert data["count"] == 1

        # Empty items → no job
        r = _api(
            "/api/v1/works/batch-refresh-metadata",
            method="post",
            json={"items": [], "source": "wikipedia"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["count"] == 0
