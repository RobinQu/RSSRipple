"""Genre unification integration tests.

Covers the closed-TMDB-genre-set feature end to end:

  - Works CRUD enum: non-canonical genre → 422; canonical → accepted
    (GenreName Literal on Create/Update renders into OpenAPI)
  - Write-back normalization: manual link with legacy/alias genre values
    ("Anime", unknown tags) → stored series genre clamped to the canonical set
  - Filter DSL: series.genre / movie.genre element-wise semantics through
    POST /agents/{id}/test-filters; wrong operator type → 422 at save
  - Mock-LLM pipeline clamping (app-llm): canned finalize genre
    ["Anime", "Action"] → stored ["Animation", "Action"]

The notification-payload genre snapshot is covered by
test_notifications.py (linked resource → payload.work.genre).

Requirements: Docker test environment (app + test-server; app-llm for the
mock-LLM class).
"""

from __future__ import annotations

import os

import httpx
import pytest

from tests.integration.http._http import (
    API_HEADERS,
    RICH_FIELD_MAPPING,
    TEST_SERVER,
    _api,
    _poll_fetch,
)

FEED_URL = f"{TEST_SERVER}/rss/mikanani?series=3"  # 咒术回战, 18 resources
LLM_APP = os.environ.get("RSSRIPPLE_LLM_URL", "")
LLM_FEED_URL = f"{TEST_SERVER}/rss/mikanani?series=0"  # 黄泉使者 (canned work)
TIMEOUT = 60.0


def _llm_api(path: str, method: str = "get", **kw) -> httpx.Response:
    c = httpx.Client(timeout=TIMEOUT, headers=API_HEADERS)
    return getattr(c, method.lower())(f"{LLM_APP}{path}", **kw)


def _and(*conditions) -> dict:
    return {"combinator": "and", "conditions": list(conditions)}


def _cond(field: str, operator: str, value=None) -> dict:
    c = {"field": field, "operator": operator}
    if value is not None:
        c["value"] = value
    return c


# =========================================================================
# Works CRUD enum validation (primary app)
# =========================================================================


class TestGenreEnumAPI:
    def test_series_genre_enum_roundtrip(self):
        r = _api(
            "/api/v1/series",
            method="post",
            json={"title_en": "Genre Enum Series", "genre": ["Animation", "Drama"]},
        )
        assert r.status_code == 201, f"create failed: {r.text}"
        sid = r.json()["data"]["id"]
        try:
            r = _api(f"/api/v1/series/{sid}")
            assert r.status_code == 200
            assert r.json()["data"]["genre"] == ["Animation", "Drama"]

            r = _api(f"/api/v1/series/{sid}", method="put", json={"genre": ["Comedy"]})
            assert r.status_code == 200, f"update failed: {r.text}"
            r = _api(f"/api/v1/series/{sid}")
            assert r.json()["data"]["genre"] == ["Comedy"]

            r = _api(
                f"/api/v1/series/{sid}", method="put", json={"genre": ["Isekai"]}
            )
            assert r.status_code == 422, "non-canonical genre should be rejected"
        finally:
            _api(f"/api/v1/series/{sid}", method="delete")

    def test_series_create_rejects_non_canonical_genre(self):
        r = _api(
            "/api/v1/series",
            method="post",
            json={"title_en": "Bad Genre Series", "genre": ["Anime", "Action"]},
        )
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"

    def test_movie_genre_enum(self):
        r = _api(
            "/api/v1/movies",
            method="post",
            json={"title_en": "Genre Enum Movie", "genre": ["Anime"]},
        )
        assert r.status_code == 422, "movie create should reject non-canonical genre"
        r = _api(
            "/api/v1/movies",
            method="post",
            json={"title_en": "Genre Enum Movie", "genre": ["Science Fiction"]},
        )
        assert r.status_code == 201, f"create failed: {r.text}"
        mid = r.json()["data"]["id"]
        try:
            r = _api(f"/api/v1/movies/{mid}")
            assert r.json()["data"]["genre"] == ["Science Fiction"]
        finally:
            _api(f"/api/v1/movies/{mid}", method="delete")

    def test_openapi_renders_genre_enum(self):
        """/docs (OpenAPI) exposes the closed genre set on works schemas."""
        r = _api("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        genre_prop = spec["components"]["schemas"]["TVSeriesCreate"]["properties"]["genre"]
        enum = genre_prop["anyOf"][0]["items"]["enum"]
        assert len(enum) == 27
        assert "Sci-Fi & Fantasy" in enum and "Animation" in enum
        # The notification detail endpoint documents the same enum.
        desc = spec["paths"]["/api/v1/notifications/{notification_id}"]["get"]["description"]
        assert "Sci-Fi & Fantasy" in desc


# =========================================================================
# Write-back normalization via manual link (primary app)
# =========================================================================


@pytest.fixture(scope="class")
def _genre_channel():
    """Channel with 18 fetched resources (metadata agent off)."""
    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "Genre Unification Channel",
            "url": FEED_URL,
            "field_mapping": RICH_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
            # filter_config 保存门禁：作品字段（series./movie.genre）仅在
            # 频道声明对应语义键后放行。
            "required_metadata_fields": ["genre"],
        },
    )
    if r.status_code != 201:
        pytest.skip(f"Channel creation failed: {r.status_code} {r.text}")
    ch_id = r.json()["data"]["id"]
    _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
    result = _poll_fetch(ch_id, accept_failed=True)
    if result.get("status") != "done":
        _api(f"/api/v1/channels/{ch_id}", method="delete")
        pytest.skip(f"Fetch did not complete: {result}")
    r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
    resources = r.json().get("data", [])
    if not resources:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
        pytest.skip("no resources fetched")
    yield {"channel_id": ch_id, "resources": resources}
    try:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
    except Exception:
        pass


class TestGenreNormalizationOnLink:
    def test_manual_link_normalizes_genre(self, _genre_channel):
        """Legacy/alias genre values are clamped to the closed set on upsert."""
        rid = _genre_channel["resources"][0]["id"]
        r = _api(
            f"/api/v1/resources/{rid}/metadata/link",
            method="put",
            json={
                "selected_result": {
                    "content_type": "tv",
                    "title_cn": "咒术回战",
                    "title_en": "Jujutsu Kaisen",
                    "external_id": "genre-test-jjk",
                    "external_source": "tmdb",
                    "genre": ["Anime", "Isekai", "action", 16],
                    "rating": 8.6,
                }
            },
        )
        assert r.status_code == 200, f"link failed: {r.text}"
        series_id = r.json()["data"]["series_id"]
        assert series_id
        try:
            r = _api(f"/api/v1/series/{series_id}")
            assert r.status_code == 200
            # "Anime"→Animation (alias), "Isekai" dropped, "action"→Action,
            # 16→Animation (TMDB id, deduped)
            assert r.json()["data"]["genre"] == ["Animation", "Action"]
        finally:
            _api(f"/api/v1/series/{series_id}", method="delete")


# =========================================================================
# Filter DSL genre fields (primary app)
# =========================================================================


class TestGenreFilterDSL:
    @pytest.fixture(scope="class")
    def _genre_agent(self, _genre_channel):
        """Agent over the genre channel; one resource linked to a genre series."""
        ch_id = _genre_channel["channel_id"]
        rid = _genre_channel["resources"][0]["id"]
        r = _api(
            f"/api/v1/resources/{rid}/metadata/link",
            method="put",
            json={
                "selected_result": {
                    "content_type": "tv",
                    "title_cn": "咒术回战",
                    "title_en": "Jujutsu Kaisen",
                    "external_id": "genre-dsl-jjk",
                    "external_source": "tmdb",
                    "genre": ["Animation", "Action"],
                }
            },
        )
        if r.status_code != 200:
            pytest.skip(f"link failed: {r.text}")
        series_id = r.json()["data"]["series_id"]

        r = _api("/api/v1/downloaders", params={"page_size": 100})
        dl_id = next(
            (d["id"] for d in r.json().get("data", []) if d.get("type") == "mock"),
            None,
        )
        if not dl_id:
            r = _api(
                "/api/v1/downloaders",
                method="post",
                json={"name": "Genre DSL Mock Downloader", "type": "mock"},
            )
            if r.status_code != 201:
                pytest.skip(f"downloader setup failed: {r.text}")
            dl_id = r.json()["data"]["id"]

        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Genre DSL Agent",
                "channel_id": ch_id,
                "downloader_id": dl_id,
                "scope_channel_wide": True,
                "llm_enabled": False,
                "conflict_resolution": "auto",
            },
        )
        if r.status_code != 201:
            pytest.skip(f"Agent creation failed: {r.text}")
        agent_id = r.json()["data"]["id"]
        yield {"agent_id": agent_id, "series_id": series_id, "linked_resource_id": rid}
        try:
            _api(f"/api/v1/agents/{agent_id}", method="delete")
            _api(f"/api/v1/series/{series_id}", method="delete")
        except Exception:
            pass

    def _passed(self, agent_id: str, filter_config: dict) -> int:
        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={"filter_config": filter_config},
        )
        assert r.status_code == 200, f"filter update failed: {r.text}"
        r = _api(f"/api/v1/agents/{agent_id}/test-filters", method="post", json={})
        assert r.status_code == 200, f"test-filters failed: {r.text}"
        return r.json()["data"]["passed"]

    def test_series_genre_contains(self, _genre_agent):
        agent_id = _genre_agent["agent_id"]
        assert self._passed(agent_id, _and(_cond("series.genre", "contains", "Animation"))) == 1
        # case-insensitive element match
        assert self._passed(agent_id, _and(_cond("series.genre", "contains", "animation"))) == 1
        assert self._passed(agent_id, _and(_cond("series.genre", "contains", "Drama"))) == 0

    def test_series_genre_in(self, _genre_agent):
        agent_id = _genre_agent["agent_id"]
        assert self._passed(
            agent_id, _and(_cond("series.genre", "in", ["Horror", "Action"]))
        ) == 1
        assert self._passed(
            agent_id, _and(_cond("series.genre", "in", ["Horror", "Drama"]))
        ) == 0

    def test_genre_empty_semantics(self, _genre_agent):
        agent_id = _genre_agent["agent_id"]
        # movie relation is absent everywhere → movie.genre is empty for all
        assert self._passed(agent_id, _and(_cond("movie.genre", "is_empty"))) == 18
        # 17 unlinked resources have no series → series.genre empty
        assert self._passed(agent_id, _and(_cond("series.genre", "is_empty"))) == 17
        assert self._passed(agent_id, _and(_cond("series.genre", "is_not_empty"))) == 1

    def test_genre_wrong_operator_422(self, _genre_agent):
        # list-of-string fields reject string-only operators (regex/fuzzy)
        r = _api(
            f"/api/v1/agents/{_genre_agent['agent_id']}",
            method="put",
            json={"filter_config": _and(_cond("series.genre", "regex", "^An"))},
        )
        assert r.status_code == 422


# =========================================================================
# Mock-LLM pipeline clamping (app-llm)
# =========================================================================


@pytest.mark.skipif(
    not LLM_APP, reason="RSSRIPPLE_LLM_URL not set (mock-LLM app not in stack)"
)
class TestGenreMockLLMClamping:
    def test_canned_genre_clamped_to_closed_set(self):
        """Canned finalize genre ["Anime", "Action"] → stored ["Animation", "Action"].

        Uses the tmdb channel source with a fake key: the search tool fails
        fast offline, and the mock LLM's canned finalize (found=true) drives
        the upsert regardless — fully deterministic. Reuses the S0 feed
        (黄泉使者) from test_llm_mock.py: whether the verdict comes from a
        fresh mock ReAct run or the gen-3 MetadataCache, the stored genre
        must already be canonical either way.
        """
        r = _llm_api(
            "/api/v1/system-settings",
            method="put",
            json={"tmdb_api_key": "mock-tmdb"},
        )
        assert r.status_code == 200, f"set fake key failed: {r.text}"
        try:
            r = _llm_api(
                "/api/v1/channels",
                method="post",
                json={
                    "name": "LLM Genre Clamp Channel",
                    "url": LLM_FEED_URL,
                    "field_mapping": RICH_FIELD_MAPPING,
                    "fetch_interval": 3600,
                    "metadata_agent_enabled": True,
                    "metadata_source": "tmdb",
                },
            )
            assert r.status_code == 201, f"create channel failed: {r.text}"
            ch_id = r.json()["data"]["id"]
        finally:
            _llm_api(
                "/api/v1/system-settings",
                method="put",
                json={"tmdb_api_key": ""},
            )
        try:
            _llm_api(f"/api/v1/channels/{ch_id}/fetch", method="post")
            deadline_result = None
            import time

            deadline = time.time() + 120
            while time.time() < deadline:
                r = _llm_api(f"/api/v1/channels/{ch_id}/fetch-status")
                deadline_result = r.json().get("data") or {}
                if deadline_result.get("status") in ("done", "failed"):
                    break
                time.sleep(2)
            assert deadline_result.get("status") == "done", f"fetch failed: {deadline_result}"

            r = _llm_api("/api/v1/series", params={"page_size": 100, "title": "黄泉使者"})
            series = [
                s for s in r.json()["data"] if s.get("external_id") == "mock-exa-daemons"
            ]
            assert series, "expected the mock-exa-daemons series to exist"
            assert series[0]["genre"] == ["Animation", "Action"], (
                f"genre should be clamped to the closed set: {series[0]['genre']}"
            )
        finally:
            _llm_api(f"/api/v1/channels/{ch_id}", method="delete")
