"""TMDB metadata search helper coverage (``metadata_search_agent``).

All HTTP surfaces (``httpx.AsyncClient`` for search_multi, ``httpx.Client``
for configuration/genre warm-up) are replaced with in-memory fakes; the
module-level result cache and the process-lifetime genre/image-base caches
are reset around every test.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services import metadata_search_agent as msa
from app.services.genre_registry import TMDB_ID_TO_NAME


@pytest.fixture(autouse=True)
def _clean_caches():
    msa._cache.clear()
    msa._TMDB_GENRE_MAP = None
    msa._tmdb_image_base.cache_clear()
    yield
    msa._cache.clear()
    msa._TMDB_GENRE_MAP = None
    msa._tmdb_image_base.cache_clear()


@pytest.fixture
def tmdb_key(monkeypatch):
    monkeypatch.setattr(
        "app.services.runtime_config._overrides", {"tmdb_api_key": "fake-tmdb-key"}
    )


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            req = httpx.Request("GET", "http://tmdb.invalid")
            raise httpx.HTTPStatusError(
                f"HTTP {self._status}", request=req, response=httpx.Response(self._status)
            )

    def json(self):
        return self._payload


class _FakeSyncClient:
    """Sync httpx.Client stand-in: maps a URL substring to a payload/exception."""

    def __init__(self, routes, *args, **kwargs):
        self._routes = routes

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        for marker, value in self._routes.items():
            if marker in url:
                if isinstance(value, Exception):
                    raise value
                return _FakeResp(value)
        raise AssertionError(f"unexpected URL: {url}")


class _FakeAsyncClient:
    def __init__(self, handler, *args, **kwargs):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None):
        return self._handler(url, params or {})


def _patch_sync_client(monkeypatch, routes):
    monkeypatch.setattr(
        msa.httpx, "Client", lambda *a, **kw: _FakeSyncClient(routes, *a, **kw)
    )


def _patch_async_client(monkeypatch, handler):
    monkeypatch.setattr(
        msa.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(handler, *a, **kw)
    )


# ---------------------------------------------------------------------------
# Session cache
# ---------------------------------------------------------------------------


def test_cache_expired_entry_is_dropped():
    msa._cache["tmdb:old"] = (0.0, [{"x": 1}])  # monotonic ts 0 ⇒ long expired
    assert msa._cache_get("tmdb", "old") is None
    assert "tmdb:old" not in msa._cache


def test_cache_evicts_oldest_at_capacity():
    for i in range(msa._CACHE_MAXSIZE):
        msa._cache[f"tmdb:t{i}"] = (float(i), [])
    msa._cache_set("tmdb", "new", [{"y": 2}])
    assert "tmdb:t0" not in msa._cache  # oldest evicted
    assert msa._cache_get("tmdb", "new") == [{"y": 2}]


def test_cache_hit_short_circuits():
    msa._cache_set("tmdb", "Cached", [{"cached": True}])
    assert msa._cache_get("tmdb", "  CACHED ") == [{"cached": True}]


# ---------------------------------------------------------------------------
# Poster URL / image base / genre map
# ---------------------------------------------------------------------------


def test_tmdb_poster_url_variants():
    assert msa._tmdb_poster_url(None) is None
    assert msa._tmdb_poster_url("") is None
    assert msa._tmdb_poster_url("/p.jpg") == "https://image.tmdb.org/t/p/w500/p.jpg"
    assert msa._tmdb_poster_url("/p.jpg", "https://img.example/") == "https://img.example/w500/p.jpg"


def test_tmdb_image_base_from_configuration(monkeypatch):
    _patch_sync_client(monkeypatch, {
        "/configuration": {"images": {"secure_base_url": "https://cdn.tmdb/"}},
    })
    assert msa._tmdb_image_base("k") == "https://cdn.tmdb/"


def test_tmdb_image_base_falls_back_on_failure(monkeypatch):
    _patch_sync_client(monkeypatch, {"/configuration": ConnectionError("down")})
    assert msa._tmdb_image_base("k") == "https://image.tmdb.org/t/p/"


def test_tmdb_genre_map_intersects_with_registry(monkeypatch):
    known_id = next(iter(TMDB_ID_TO_NAME))
    _patch_sync_client(monkeypatch, {
        "/genre/tv/list": {"genres": [{"id": known_id, "name": "X"}, {"id": 999999, "name": "Bogus"}]},
        "/genre/movie/list": {"genres": []},
    })
    result = msa._tmdb_genre_map("k")
    # Registry-only intersection: the bogus id is dropped, the known id kept
    # under the registry's canonical name (not TMDB's raw name).
    assert result == {known_id: TMDB_ID_TO_NAME[known_id]}
    # A second call serves the warm process-lifetime cache without HTTP.
    _patch_sync_client(monkeypatch, {})
    assert msa._tmdb_genre_map("k") == result


def test_tmdb_genre_map_static_fallback_on_failure(monkeypatch):
    _patch_sync_client(monkeypatch, {"/genre/tv/list": ConnectionError("down")})
    assert msa._tmdb_genre_map("k") == dict(TMDB_ID_TO_NAME)


def test_tmdb_genre_map_empty_response_falls_back(monkeypatch):
    _patch_sync_client(monkeypatch, {
        "/genre/tv/list": {"genres": []},
        "/genre/movie/list": {"genres": []},
    })
    assert msa._tmdb_genre_map("k") == dict(TMDB_ID_TO_NAME)


def test_resolve_genre_ids(monkeypatch):
    monkeypatch.setattr(msa, "_tmdb_genre_map", lambda api_key: {28: "Action"})
    assert msa._resolve_genre_ids([], "k") == []
    assert msa._resolve_genre_ids([28, 999], "k") == ["Action"]
    # Unhashable element must be skipped with a warning, not crash.
    assert msa._resolve_genre_ids([[28], 28], "k") == ["Action"]


# ---------------------------------------------------------------------------
# _search_tmdb
# ---------------------------------------------------------------------------


async def test_search_tmdb_without_api_key(monkeypatch):
    monkeypatch.setattr("app.services.runtime_config._overrides", {"tmdb_api_key": ""})

    def boom(url, params):
        raise AssertionError("HTTP must not be attempted without an API key")

    _patch_async_client(monkeypatch, boom)
    assert await msa._search_tmdb("Anything") == []


async def test_search_tmdb_returns_cached_results(tmdb_key, monkeypatch):
    msa._cache_set("tmdb", "cached title", [{"cached": True}])

    def boom(url, params):
        raise AssertionError("cache hit must not trigger HTTP")

    _patch_async_client(monkeypatch, boom)
    assert await msa._search_tmdb("Cached Title") == [{"cached": True}]


def _tv_item(**over):
    item = {
        "media_type": "tv",
        "id": 101,
        "name": "中文剧名",
        "original_name": "Original Show",
        "overview": "简介",
        "poster_path": "/p.jpg",
        "vote_average": 8.5,
        "genre_ids": [16],
        "original_language": "ja",
        "origin_country": ["JP"],
        "first_air_date": "2020-04-05",
    }
    item.update(over)
    return item


async def test_search_tmdb_merges_languages_and_builds_candidates(tmdb_key, monkeypatch):
    def handler(url, params):
        if params["language"] == "zh-CN":
            return _FakeResp({"results": [
                _tv_item(),
                _tv_item(id=202, media_type="movie", name="电影名",
                         first_air_date=None, release_date="2021-07-01",
                         genre_ids=[28], original_language="en",
                         origin_country=["US"], vote_average=6.0),
                # First-seen item missing optional fields; the en-US pass
                # backfills genre_ids/original_language/origin_country.
                _tv_item(id=505, name="五零五", original_name=None,
                         genre_ids=None, original_language=None,
                         origin_country=None, vote_average=4.0),
                {"media_type": "person", "id": 303},          # wrong media type → skip
                {"media_type": "tv"},                          # missing id → skip
                _tv_item(id=404, name="", original_name=""),   # no title → filtered out
            ]})
        return _FakeResp({"results": [
            _tv_item(name="English Show", original_name=None),
            _tv_item(id=505, name="Five Oh Five", original_name="Orig Title 505",
                     genre_ids=[28], original_language="en", origin_country=["US"],
                     vote_average=4.0),
        ]})

    _patch_async_client(monkeypatch, handler)
    monkeypatch.setattr(msa, "_tmdb_image_base", lambda api_key: "https://img.tmdb/")
    monkeypatch.setattr(msa, "_tmdb_genre_map", lambda api_key: {16: "Animation", 28: "Action"})

    candidates = await msa._search_tmdb("some title")

    # Sorted by vote_average desc; the title-less candidate is filtered.
    assert [c["external_id"] for c in candidates] == ["tmdb:101", "tmdb:202", "tmdb:505"]
    tv = candidates[0]
    assert tv["title_cn"] == "中文剧名"          # zh-CN slot
    assert tv["title_en"] == "English Show"      # merged from en-US result
    assert tv["original_title"] == "Original Show"
    assert tv["content_type"] == "tv"
    assert tv["year"] == 2020
    assert tv["start_date"] == "2020-04-05"
    assert tv["genre"] == ["Animation"]
    assert tv["poster_url"] == "https://img.tmdb/w500/p.jpg"
    assert tv["is_anime"] is True                # Animation + ja/JP
    movie = candidates[1]
    assert movie["release_date"] == "2021-07-01"
    assert movie["year"] == 2021
    assert movie["genre"] == ["Action"]
    assert movie["is_anime"] is False            # genre present, no Animation
    backfilled = candidates[2]
    assert backfilled["title_cn"] == "五零五"    # zh-CN slot from the zh pass
    assert backfilled["title_en"] == "Five Oh Five"
    # Optional fields absent from the zh item were backfilled by the en pass.
    assert backfilled["original_title"] == "Orig Title 505"
    assert backfilled["genre"] == ["Action"]

    # Result is cached: a second call must not hit HTTP again.
    _patch_async_client(monkeypatch, lambda url, params: (_ for _ in ()).throw(AssertionError))
    assert await msa._search_tmdb("some title") == candidates


async def test_search_tmdb_http_errors_yield_no_results(tmdb_key, monkeypatch):
    def handler(url, params):
        if params["language"] == "zh-CN":
            req = httpx.Request("GET", "http://tmdb.invalid")
            raise httpx.HTTPStatusError(
                "boom", request=req, response=httpx.Response(500)
            )
        raise httpx.TimeoutException("slow")

    _patch_async_client(monkeypatch, handler)
    assert await msa._search_tmdb("err title") == []


async def test_search_tmdb_unexpected_error_yield_no_results(tmdb_key, monkeypatch):
    def handler(url, params):
        raise ValueError("weird payload")

    _patch_async_client(monkeypatch, handler)
    assert await msa._search_tmdb("weird title") == []


async def test_search_tmdb_empty_merge_is_cached(tmdb_key, monkeypatch):
    calls = {"n": 0}

    def handler(url, params):
        calls["n"] += 1
        return _FakeResp({"results": [{"media_type": "person", "id": 1}]})

    _patch_async_client(monkeypatch, handler)
    assert await msa._search_tmdb("person only") == []
    assert await msa._search_tmdb("person only") == []
    assert calls["n"] == 2  # one zh + one en call; the second search hit the cache


async def test_search_tmdb_task_level_base_exception(tmdb_key, monkeypatch):
    """If a search task itself blows up (BaseException escapes the per-lang
    error handling), gather returns it and the side is treated as empty."""
    def failing_create_task(coro):
        coro.close()  # never scheduled — avoid "never awaited" warnings
        fut = asyncio.get_running_loop().create_future()
        fut.set_exception(RuntimeError("task exploded"))
        return fut

    monkeypatch.setattr(msa.asyncio, "create_task", failing_create_task)
    assert await msa._search_tmdb("task failure") == []
