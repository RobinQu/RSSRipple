"""Supplementary integration coverage tests.

Targets integration-coverage gaps not exercised by the focused suites:

  - WorkCollection CRUD + attach/detach + works subresource + detail
    collection_siblings (app/api/v1/collections.py,
    app/services/collection_service.collection_work_summaries)
  - Deterministic TMDB collection link error path: manual movie link with a
    canonical ``tmdb:<digits>`` external id while a fake TMDB key is
    configured — the TMDB details request is built and fails fast
    (collection_service.link_movie_collection / fetch_tmdb_movie_collection)
  - API key lifecycle: create / list / authenticate / delete
    (app/api/v1/api_keys.py, app/services/auth_service.py)
  - refresh-metadata with tmdb/jina sources against the mock-LLM app with
    fake source keys — the per-source search helpers run their
    request-building code and fail fast on the unreachable/401 external call
    (app/services/metadata_source_io.py)

Requirements: Docker test environment (app + app-llm + test-server).
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from tests.integration.http._http import (
    API_HEADERS,
    RSSRIPPLE,
    TEST_SERVER,
    TIMEOUT,
    _api,
    _poll_fetch,
    associate_metadata_request,
    ensure_series,
    refresh_work_metadata,
)

LLM_APP = os.environ.get("RSSRIPPLE_LLM_URL", "")

MOVIES_FEED_URL = f"{TEST_SERVER}/rss/movies"

# Field mapping for the movie feed: scene titles carry no episode number and
# the torrent lives in the enclosure, not the link (a GUID page).
MOVIE_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_raw": {"source": "title"},
        "torrent_url": {"source": "enclosures[0].url"},
    },
}


def _llm_api(path: str, method: str = "get", timeout: float = TIMEOUT, **kw) -> httpx.Response:
    """HTTP call against the mock-LLM app instance."""
    c = httpx.Client(timeout=timeout, headers=API_HEADERS)
    return getattr(c, method.lower())(f"{LLM_APP}{path}", **kw)


def _raw(path: str, headers: dict | None, method: str = "get", **kw) -> httpx.Response:
    """HTTP call against the primary app with explicit (or no) auth headers."""
    c = httpx.Client(timeout=TIMEOUT)
    return getattr(c, method.lower())(f"{RSSRIPPLE}{path}", headers=headers or {}, **kw)


def _ensure_movie(title_cn: str, title_en: str, **extra) -> str:
    """Get-or-create a movie by exact title_cn; returns the movie id."""
    r = _api("/api/v1/movies", params={"page_size": 100, "title": title_cn})
    if r.status_code == 200:
        for m in r.json().get("data", []):
            if m.get("title_cn") == title_cn:
                return m["id"]
    r = _api(
        "/api/v1/movies",
        method="post",
        json={"title_cn": title_cn, "title_en": title_en, **extra},
    )
    assert r.status_code == 201, f"Movie creation failed: {r.status_code} {r.text}"
    return r.json()["data"]["id"]


def _create_collection(title_cn: str, **extra) -> str:
    r = _api("/api/v1/collections", method="post", json={"title_cn": title_cn, **extra})
    assert r.status_code == 201, f"create collection failed: {r.text}"
    return r.json()["data"]["id"]


def _quiet_delete(path: str) -> None:
    try:
        _api(path, method="delete")
    except Exception:
        pass


def _ensure_detached(work_type: str, work_id: str, api=_api) -> None:
    """Detach a work from whatever collection it currently belongs to.

    Keeps the attach tests idempotent across reruns: a previously interrupted
    run may have left the work grouped, and attaching an occupied work to a
    new collection is a 409.
    """
    detail_path = "series" if work_type == "series" else "movies"
    r = api(f"/api/v1/{detail_path}/{work_id}")
    if r.status_code != 200:
        return
    coll = (r.json().get("data") or {}).get("collection") or {}
    cid = coll.get("id")
    if cid:
        api(
            f"/api/v1/collections/{cid}/works/{work_id}",
            method="delete",
            params={"work_type": work_type},
        )


# =========================================================================
# API keys — auth_service.create_api_key / check_api_key + api_keys router
# =========================================================================


class TestApiKeyLifecycle:
    def test_missing_credentials_401(self):
        r = _raw("/api/v1/works", headers=None)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHORIZED"

    def test_wrong_key_401(self):
        r = _raw("/api/v1/works", headers={"X-API-Key": "rr_no-such-key"})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHORIZED"

    def test_full_lifecycle(self):
        name = f"coverage-key-{int(time.time())}"
        key_id = None
        try:
            # Create — plaintext returned exactly once.
            r = _api("/api/v1/api-keys", method="post", json={"name": name})
            assert r.status_code == 201, f"create api key failed: {r.text}"
            data = r.json()["data"]
            key_id = data["id"]
            plaintext = data["key"]
            assert plaintext.startswith("rr_")
            assert data["prefix"] == plaintext[:10]

            # List — never exposes the plaintext again.
            r = _api("/api/v1/api-keys")
            assert r.status_code == 200
            items = [k for k in r.json()["data"] if k["id"] == key_id]
            assert items and items[0]["name"] == name
            assert "key" not in items[0] and "key_hash" not in items[0]

            # The key authenticates via both header lanes.
            r = _raw("/api/v1/works", headers={"X-API-Key": plaintext})
            assert r.status_code == 200, f"X-API-Key auth failed: {r.text}"
            r = _raw(
                "/api/v1/works",
                headers={"Authorization": f"Bearer {plaintext}"},
            )
            assert r.status_code == 200, f"Bearer auth failed: {r.text}"

            # Delete → the key stops working; second delete 404s.
            r = _api(f"/api/v1/api-keys/{key_id}", method="delete")
            assert r.status_code == 200, f"delete api key failed: {r.text}"
            assert r.json()["data"]["deleted"] is True
            key_id = None

            r = _raw("/api/v1/works", headers={"X-API-Key": plaintext})
            assert r.status_code == 401
        finally:
            if key_id:
                _quiet_delete(f"/api/v1/api-keys/{key_id}")

    def test_delete_unknown_key_404(self):
        r = _api("/api/v1/api-keys/nonexistent-key-id", method="delete")
        assert r.status_code == 404


# =========================================================================
# WorkCollections — CRUD, attach/detach, works subresource, detail siblings
# =========================================================================


class TestCollections:
    def test_crud_flow(self):
        cid = _create_collection("覆盖率合集·基本", description="初始描述")
        try:
            # List + fuzzy search + work_count.
            r = _api("/api/v1/collections", params={"search": "覆盖率合集·基本"})
            assert r.status_code == 200
            rows = [c for c in r.json()["data"] if c["id"] == cid]
            assert rows and rows[0]["work_count"] == 0

            r = _api("/api/v1/collections", params={"page": 1, "page_size": 1})
            assert r.status_code == 200
            assert len(r.json()["data"]) == 1
            assert r.json()["meta"]["total"] >= 1

            # Detail with empty works.
            r = _api(f"/api/v1/collections/{cid}")
            assert r.status_code == 200
            assert r.json()["data"]["works"] == []

            # Update.
            r = _api(
                f"/api/v1/collections/{cid}",
                method="patch",
                json={"title_cn": "覆盖率合集·改名", "description": "新描述"},
            )
            assert r.status_code == 200
            assert r.json()["data"]["title_cn"] == "覆盖率合集·改名"
            assert r.json()["data"]["description"] == "新描述"

            # Delete.
            r = _api(f"/api/v1/collections/{cid}", method="delete")
            assert r.status_code == 200
            assert r.json()["data"]["deleted"] is True
            deleted_cid, cid = cid, None
            assert _api(f"/api/v1/collections/{deleted_cid}").status_code == 404
        finally:
            if cid:
                _quiet_delete(f"/api/v1/collections/{cid}")

    def test_not_found_paths(self):
        assert _api("/api/v1/collections/nope").status_code == 404
        assert _api("/api/v1/collections/nope/works").status_code == 404
        r = _api("/api/v1/collections/nope", method="patch", json={"title_cn": "x"})
        assert r.status_code == 404
        assert _api("/api/v1/collections/nope", method="delete").status_code == 404
        r = _api(
            "/api/v1/collections/nope/works",
            method="post",
            json={"work_type": "series", "work_id": "x"},
        )
        assert r.status_code == 404
        r = _api(
            "/api/v1/collections/nope/works/x",
            method="delete",
            params={"work_type": "series"},
        )
        assert r.status_code == 404

    def test_attach_detach_and_siblings(self):
        cid = _create_collection("覆盖率合集·挂载")
        cid2 = _create_collection("覆盖率合集·占位")
        sid = ensure_series("覆盖率合集剧集", "Coverage Collection Series")
        mid = _ensure_movie(
            "覆盖率合集电影", "Coverage Collection Movie", release_date="2024-05-01"
        )
        # A previously interrupted run may have left the works grouped.
        _ensure_detached("series", sid)
        _ensure_detached("movie", mid)
        try:
            # Attach series + movie (idempotent re-attach included).
            for work_type, work_id in (("series", sid), ("movie", mid)):
                r = _api(
                    f"/api/v1/collections/{cid}/works",
                    method="post",
                    json={"work_type": work_type, "work_id": work_id},
                )
                assert r.status_code == 201, f"attach {work_type} failed: {r.text}"
                assert r.json()["data"]["attached"] is True
            r = _api(
                f"/api/v1/collections/{cid}/works",
                method="post",
                json={"work_type": "series", "work_id": sid},
            )
            assert r.status_code == 201, f"re-attach should be idempotent: {r.text}"

            # Occupied work → 409 on a second collection.
            r = _api(
                f"/api/v1/collections/{cid2}/works",
                method="post",
                json={"work_type": "movie", "work_id": mid},
            )
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "DUPLICATE_SUBMISSION"

            # Validation errors.
            r = _api(
                f"/api/v1/collections/{cid}/works",
                method="post",
                json={"work_type": "book", "work_id": sid},
            )
            assert r.status_code == 422
            assert r.json()["error"]["code"] == "VALIDATION_ERROR"
            r = _api(
                f"/api/v1/collections/{cid}/works",
                method="post",
                json={"work_type": "movie", "work_id": "no-such-work"},
            )
            assert r.status_code == 404

            # Detail: works summaries cover both tables.
            r = _api(f"/api/v1/collections/{cid}")
            assert r.status_code == 200
            works = r.json()["data"]["works"]
            assert {w["id"] for w in works} == {sid, mid}
            assert {w["type"] for w in works} == {"series", "movie"}
            by_id = {w["id"]: w for w in works}
            assert by_id[mid]["year"] == 2024

            # Works subresource: normalized shape with collection fields.
            r = _api(f"/api/v1/collections/{cid}/works", params={"page_size": 100})
            assert r.status_code == 200
            assert r.json()["meta"]["total"] == 2
            for item in r.json()["data"]:
                assert item["collection_id"] == cid
                assert item["collection_name"] == "覆盖率合集·挂载"
                assert item["content_type"] in ("tv", "movie")

            # List row reports the member count.
            r = _api("/api/v1/collections", params={"search": "覆盖率合集·挂载"})
            row = next(c for c in r.json()["data"] if c["id"] == cid)
            assert row["work_count"] == 2

            # Work detail pages surface collection + siblings (exclude self).
            r = _api(f"/api/v1/series/{sid}")
            assert r.status_code == 200
            detail = r.json()["data"]
            assert detail["collection"]["id"] == cid
            sibling_ids = {w["id"] for w in detail["collection_siblings"]}
            assert mid in sibling_ids and sid not in sibling_ids

            r = _api(f"/api/v1/movies/{mid}")
            assert r.status_code == 200
            detail = r.json()["data"]
            assert detail["collection"]["id"] == cid
            sibling_ids = {w["id"] for w in detail["collection_siblings"]}
            assert sid in sibling_ids and mid not in sibling_ids

            # Poster wall collection filter (and the 'none' literal).
            r = _api("/api/v1/works", params={"collection_id": cid, "page_size": 100})
            assert r.status_code == 200
            assert {w["id"] for w in r.json()["data"]} == {sid, mid}
            r = _api("/api/v1/works", params={"collection_id": "none", "page_size": 100})
            assert r.status_code == 200
            assert sid not in {w["id"] for w in r.json()["data"]}

            # Detach: second detach 404s; invalid work_type 422s.
            r = _api(
                f"/api/v1/collections/{cid}/works/{sid}",
                method="delete",
                params={"work_type": "series"},
            )
            assert r.status_code == 200
            assert r.json()["data"]["detached"] is True
            r = _api(
                f"/api/v1/collections/{cid}/works/{sid}",
                method="delete",
                params={"work_type": "series"},
            )
            assert r.status_code == 404
            r = _api(
                f"/api/v1/collections/{cid}/works/{sid}",
                method="delete",
                params={"work_type": "book"},
            )
            assert r.status_code == 422

            r = _api(f"/api/v1/series/{sid}")
            assert r.json()["data"]["collection"] is None
        finally:
            # Deleting the collection detaches remaining member works.
            _quiet_delete(f"/api/v1/collections/{cid}")
            _quiet_delete(f"/api/v1/collections/{cid2}")
            r = _api(f"/api/v1/movies/{mid}")
            if r.status_code == 200:
                assert r.json()["data"]["collection"] is None


# =========================================================================
# Deterministic TMDB collection link — error path with a fake TMDB key
# =========================================================================


@pytest.fixture(scope="class")
def _movies_channel():
    """Movie-feed channel on the primary app, with a fake TMDB key configured
    (restored afterwards) so the deterministic collection-link path builds its
    TMDB details request and fails fast."""
    r = _api(
        "/api/v1/system-settings",
        method="put",
        json={"tmdb_api_key": "mock-tmdb-coverage"},
    )
    assert r.status_code == 200, f"set fake tmdb key failed: {r.text}"
    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "Coverage Movie Link Channel",
            "url": MOVIES_FEED_URL,
            "field_mapping": MOVIE_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    assert r.status_code == 201, f"create channel failed: {r.text}"
    ch_id = r.json()["data"]["id"]
    try:
        _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
        result = _poll_fetch(ch_id, accept_failed=True)
        assert result.get("status") == "done", f"fetch failed: {result}"
        r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
        resources = r.json().get("data", [])
        if not resources:
            pytest.skip("no resources after movie-feed fetch")
        yield resources
    finally:
        _quiet_delete(f"/api/v1/channels/{ch_id}")
        _api("/api/v1/system-settings", method="put", json={"tmdb_api_key": ""})


class TestTmdbCollectionLink:
    """Manual movie link with a canonical tmdb: id while the TMDB details
    endpoint is unreachable/401 (fake key) — link_movie_collection builds the
    request, fails fast, and leaves the movie ungrouped."""

    movie_id: str = ""

    def test_manual_link_movie_tmdb_fetch_fails_fast(self, _movies_channel):
        # A previously interrupted run may have left the movie grouped.
        r = _api("/api/v1/movies", params={"page_size": 100, "title": "合集链接覆盖电影"})
        for m in r.json().get("data", []):
            if m.get("title_cn") == "合集链接覆盖电影":
                _ensure_detached("movie", m["id"])

        res = _movies_channel[0]
        r = associate_metadata_request(
            f"/api/v1/resources/{res['id']}/metadata/link",
            method="put",
            json={
                "selected_result": {
                    "content_type": "movie",
                    "title_cn": "合集链接覆盖电影",
                    "title_en": "Coverage Collection Link Movie",
                    "description": "coverage: deterministic tmdb collection link",
                    "external_id": "tmdb:999983",
                    "external_source": "tmdb",
                    "year": 2024,
                }
            },
        )
        assert r.status_code == 200, f"movie link failed: {r.text}"
        data = r.json()["data"]
        assert data["movie_id"], "resource should be movie-linked"
        TestTmdbCollectionLink.movie_id = data["movie_id"]

        # TMDB details request failed (fake key) → no collection attached.
        r = _api(f"/api/v1/movies/{data['movie_id']}")
        assert r.status_code == 200
        assert r.json()["data"]["collection"] is None

    def test_relink_attached_movie_skips_collection_fetch(self, _movies_channel):
        """link_movie_collection no-ops when the movie is already grouped."""
        if not TestTmdbCollectionLink.movie_id:
            pytest.skip("no linked movie — prerequisite failed")
        mid = TestTmdbCollectionLink.movie_id
        cid = _create_collection("覆盖率合集·已挂载")
        try:
            r = _api(
                f"/api/v1/collections/{cid}/works",
                method="post",
                json={"work_type": "movie", "work_id": mid},
            )
            assert r.status_code == 201, f"attach failed: {r.text}"

            # Re-link another resource to the same external entity: the movie
            # update path hits link_movie_collection's already-linked early
            # return instead of fetching TMDB again.
            res = _movies_channel[1] if len(_movies_channel) > 1 else _movies_channel[0]
            r = associate_metadata_request(
                f"/api/v1/resources/{res['id']}/metadata/link",
                method="put",
                json={
                    "selected_result": {
                        "content_type": "movie",
                        "title_cn": "合集链接覆盖电影",
                        "title_en": "Coverage Collection Link Movie",
                        "external_id": "tmdb:999983",
                        "external_source": "tmdb",
                    }
                },
            )
            assert r.status_code == 200, f"re-link failed: {r.text}"
            assert r.json()["data"]["movie_id"] == mid

            # Grouping is preserved across the re-link.
            r = _api(f"/api/v1/movies/{mid}")
            assert r.json()["data"]["collection"]["id"] == cid
        finally:
            # Deleting the collection detaches the movie (NULL collection_id)
            # so a rerun starts from the same ungrouped state.
            _quiet_delete(f"/api/v1/collections/{cid}")


# =========================================================================
# refresh-metadata against mock-LLM app — per-source search I/O error paths
# =========================================================================


@pytest.fixture(scope="class")
def _fake_source_keys():
    """Configure dummy external-source keys on app-llm (restored after)."""
    r = _llm_api(
        "/api/v1/system-settings",
        method="put",
        json={
            "tmdb_api_key": "mock-tmdb-coverage",
            "jina_api_key": "mock-jina-coverage",
        },
    )
    assert r.status_code == 200, f"set fake keys failed: {r.text}"
    yield
    _llm_api(
        "/api/v1/system-settings",
        method="put",
        json={"tmdb_api_key": "", "jina_api_key": ""},
    )


@pytest.mark.skipif(not LLM_APP, reason="RSSRIPPLE_LLM_URL not set (mock-LLM app not in stack)")
class TestMetadataSourceRefresh:
    """refresh-metadata with tmdb/jina on app-llm. Fake keys make the
    per-source search helpers build their requests and fail fast; the mock
    LLM then finalizes deterministically."""

    def _refresh(self, series_id: str, source: str) -> dict:
        r = refresh_work_metadata(
            series_id, "tv", source, api=_llm_api,
        )
        assert r.status_code == 200, f"refresh ({source}) failed: {r.text}"
        data = r.json()["data"]
        assert data["source"] == source
        assert "found" in data and "filled" in data
        return data

    def test_refresh_tmdb_source_not_found(self, _fake_source_keys):
        """tmdb ReAct: search_tmdb tool errors (fake key) → found=False."""
        sid = ensure_series("覆盖率检索不到剧集", "Coverage Unfindable Series", api=_llm_api)
        data = self._refresh(sid, "tmdb")
        assert data["filled"] == []

    def test_refresh_jina_source_not_found(self, _fake_source_keys):
        """Deprecated jina is rejected by the unified search contract."""
        r = _llm_api(
            "/api/v1/metadata/search",
            method="post",
            json={
                "query": "Coverage Unfindable Series",
                "content_type": "tv",
                "mode": "online",
                "source": "jina",
            },
        )
        assert r.status_code == 422

    def test_tmdb_canned_search_returns_candidate(self, _fake_source_keys):
        """The unified TMDB search returns the mock LLM's canned candidate."""
        r = _llm_api(
            "/api/v1/metadata/search",
            method="post",
            json={
                "query": "黄泉使者",
                "content_type": "tv",
                "mode": "online",
                "source": "tmdb",
            },
        )
        assert r.status_code == 200, r.text
        candidates = r.json()["data"]["candidates"]
        assert candidates
        assert candidates[0]["external_id"] == "mock-exa-daemons"
