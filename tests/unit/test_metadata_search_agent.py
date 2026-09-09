"""Unit tests for the TMDB search helpers in ``metadata_search_agent``.

These are pure-Python helpers (plus one async search coroutine) that run a
single-source TMDB search and emit uniform candidate dicts for the metadata
pipeline. The sync httpx fetches and the genre/image warm-ups are mocked so
we can drive every branch deterministically.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import app.services.metadata_search_agent as msa

_OVERRIDES = "app.services.runtime_config._overrides"


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Reset the module-level LRU cache and genre map between tests."""
    msa._cache.clear()
    msa._TMDB_GENRE_MAP = None
    msa._tmdb_image_base.cache_clear()
    yield
    msa._cache.clear()
    msa._TMDB_GENRE_MAP = None
    msa._tmdb_image_base.cache_clear()


def _httpx_client_mock(results_by_url):
    """Return a MagicMock suitable for ``httpx.AsyncClient`` that responds
    per requested URL or per ``language`` param (used to simulate the two
    parallel language searches)."""

    def _client(*a, **kw):
        client = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None

        async def _get(url, **kw):
            lang = (kw.get("params") or {}).get("language")
            if lang in results_by_url:
                resp.json.return_value = results_by_url[lang]
            else:
                resp.json.return_value = results_by_url[url.split("/")[-1]]
            return resp

        client.get = AsyncMock(side_effect=_get)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    return MagicMock(side_effect=_client)


def _sync_client_mock(results_by_url, raise_error=False):
    """Mock for the sync ``httpx.Client`` used by image-base/genre warm-ups.
    Responds per URL; with ``raise_error`` the HTTP call raises instead."""

    def _get(url, **kw):
        resp = MagicMock()
        if raise_error:
            resp.raise_for_status.side_effect = RuntimeError("boom")
        else:
            resp.raise_for_status.return_value = None
            if "/genre/tv/" in url:
                key = "tv"
            elif "/genre/movie/" in url:
                key = "movie"
            else:
                key = url.split("/")[-1]
            resp.json.return_value = results_by_url[key]
        return resp

    client = MagicMock()
    client.get = MagicMock(side_effect=_get)
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# Session cache
# ---------------------------------------------------------------------------


def test_cache_key_normalizes_title():
    assert msa._cache_key("tmdb", "  Attack on Titan  ") == "tmdb:attack on titan"


def test_cache_get_miss_and_expiry():
    msa._cache["tmdb:show"] = (time.monotonic() - 7200, [{"id": 1}])
    assert msa._cache_get("tmdb", "show") is None
    assert "tmdb:show" not in msa._cache

    msa._cache["tmdb:show"] = (time.monotonic(), [{"id": 1}])
    assert msa._cache_get("tmdb", "show") == [{"id": 1}]


def test_cache_set_and_get_roundtrip():
    msa._cache_set("tmdb", " Roundtrip ", [{"id": 9}])
    assert msa._cache_get("tmdb", "roundtrip") == [{"id": 9}]


def test_cache_set_evicts_oldest_at_capacity(monkeypatch):
    monkeypatch.setattr(msa, "_CACHE_MAXSIZE", 2)
    msa._cache_set("tmdb", "one", [{"id": 1}])
    msa._cache_set("tmdb", "two", [{"id": 2}])
    msa._cache_set("tmdb", "three", [{"id": 3}])
    assert "tmdb:one" not in msa._cache
    assert "tmdb:two" in msa._cache
    assert "tmdb:three" in msa._cache


# ---------------------------------------------------------------------------
# TMDB poster / image base
# ---------------------------------------------------------------------------


def test_tmdb_poster_url():
    assert msa._tmdb_poster_url(None) is None
    assert msa._tmdb_poster_url("") is None
    assert msa._tmdb_poster_url("/p.jpg") == "https://image.tmdb.org/t/p/w500/p.jpg"
    assert msa._tmdb_poster_url("/p.jpg", "https://img/") == "https://img/w500/p.jpg"


def test_tmdb_image_base_success():
    client = _sync_client_mock({"configuration": {"images": {"secure_base_url": "https://cdn/"}}})
    with patch("httpx.Client", MagicMock(return_value=client)):
        assert msa._tmdb_image_base("k") == "https://cdn/"


def test_tmdb_image_base_failure_falls_back():
    client = _sync_client_mock({}, raise_error=True)
    with patch("httpx.Client", MagicMock(return_value=client)):
        assert msa._tmdb_image_base("k") == "https://image.tmdb.org/t/p/"


# ---------------------------------------------------------------------------
# Genre map + resolution
# ---------------------------------------------------------------------------


def test_tmdb_genre_map_cached(monkeypatch):
    msa._TMDB_GENRE_MAP = {18: "Drama"}
    with patch("httpx.Client") as mock_client:
        assert msa._tmdb_genre_map("k") == {18: "Drama"}
    mock_client.assert_not_called()


def test_tmdb_genre_map_success_intersects_registry():
    client = _sync_client_mock(
        {
            "tv": {"genres": [{"id": 18, "name": "Drama"}, {"id": 99999, "name": "Bogus"}]},
            "movie": {"genres": [{"id": 28, "name": "Action"}]},
        }
    )
    with patch("httpx.Client", MagicMock(return_value=client)):
        result = msa._tmdb_genre_map("k")
    # 99999 is outside the closed registry so it is dropped.
    assert result == {18: "Drama", 28: "Action"}


def test_tmdb_genre_map_empty_falls_back_to_registry():
    client = _sync_client_mock({"tv": {"genres": []}, "movie": {"genres": []}})
    with patch("httpx.Client", MagicMock(return_value=client)):
        result = msa._tmdb_genre_map("k")
    assert result == dict(msa.TMDB_ID_TO_NAME)


def test_tmdb_genre_map_error_falls_back_to_registry():
    client = _sync_client_mock({}, raise_error=True)
    with patch("httpx.Client", MagicMock(return_value=client)):
        result = msa._tmdb_genre_map("k")
    assert result == dict(msa.TMDB_ID_TO_NAME)


def test_resolve_genre_ids():
    with patch.object(msa, "_tmdb_genre_map", return_value={18: "Drama", 28: "Action"}):
        assert msa._resolve_genre_ids([], "k") == []
        assert msa._resolve_genre_ids([18, 28, 99], "k") == ["Drama", "Action"]


def test_resolve_genre_ids_unhashable_element(caplog):
    with patch.object(msa, "_tmdb_genre_map", return_value={18: "Drama"}):
        # A list is unhashable → the TypeError branch warns instead of crashing.
        assert msa._resolve_genre_ids([18, ["bogus"]], "k") == ["Drama"]


# ---------------------------------------------------------------------------
# _search_tmdb
# ---------------------------------------------------------------------------


async def test_search_tmdb_no_api_key_returns_empty():
    with patch.dict(_OVERRIDES, {"tmdb_api_key": ""}):
        assert await msa._search_tmdb("Show") == []


async def test_search_tmdb_hits_cache():
    client = _sync_async_client_for({"results": []})
    with (
        patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
        patch.object(msa, "_cache_get", return_value=[{"id": 1}]),
        patch("httpx.AsyncClient", MagicMock(return_value=client)) as http_mock,
    ):
        assert await msa._search_tmdb("Show") == [{"id": 1}]
    http_mock.assert_not_called()


async def test_search_tmdb_merges_both_languages():
    results = {
        "multi": {
            "results": [
                {
                    "media_type": "tv", "id": 1,
                    "name": "剧集", "original_name": "Show",
                    "overview": "synopsis zh", "poster_path": "/p.jpg",
                    "vote_average": 8.0, "genre_ids": [18],
                    "original_language": "ja", "origin_country": ["JP"],
                    "first_air_date": "2020-01-01",
                }
            ]
        }
    }
    # Both zh-CN and en-US searches share the same URL path → same response;
    # the merge must pick up title_en from the en-US pass.
    with (
        patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", _httpx_client_mock(results)),
        patch.object(msa, "_tmdb_image_base", return_value="https://img/"),
        patch.object(msa, "_tmdb_genre_map", return_value={18: "Drama"}),
        patch.object(msa, "_cache_set") as cache_set,
    ):
        candidates = await msa._search_tmdb("Show")

    assert len(candidates) == 1
    c = candidates[0]
    assert c["content_type"] == "tv"
    assert c["title_cn"] == "剧集"
    assert c["title_en"] == "剧集"
    assert c["original_title"] == "Show"
    assert c["year"] == 2020
    assert c["genre"] == ["Drama"]
    assert c["status"] is None  # no "status" key in the raw item
    assert c["external_id"] == "tmdb:1"
    assert c["external_source"] == "tmdb"
    assert c["poster_url"] == "https://img/w500/p.jpg"
    cache_set.assert_called_once()


async def test_search_tmdb_movie_branch_and_status():
    results = {
        "multi": {
            "results": [
                {
                    "media_type": "movie", "id": 7,
                    "title": "电影", "original_title": "Film",
                    "release_date": "2019-05-01", "status": "Released",
                    "genre_ids": [], "vote_average": 7.0,
                }
            ]
        }
    }
    with (
        patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", _httpx_client_mock(results)),
        patch.object(msa, "_tmdb_image_base", return_value="https://img/"),
        patch.object(msa, "_tmdb_genre_map", return_value={}),
    ):
        candidates = await msa._search_tmdb("Film")

    assert len(candidates) == 1
    c = candidates[0]
    assert c["content_type"] == "movie"
    assert c["year"] == 2019
    # The multi-search result carries no status → status stays None (the map
    # branches run but find nothing).
    assert c["status"] is None
    assert c["release_date"] == "2019-05-01"
    assert c["start_date"] is None


async def test_search_tmdb_language_error_falls_back_to_empty():
    client = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.side_effect = RuntimeError("connection refused")
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", MagicMock(return_value=client)),
        patch.object(msa, "_cache_set") as cache_set,
    ):
        candidates = await msa._search_tmdb("Boom")

    assert candidates == []
    # No results → empty result is cached.
    cache_set.assert_called_once_with("tmdb", "Boom", [])


async def test_search_tmdb_language_timeout_returns_empty():
    """HTTPStatusError/TimeoutException inside _search_lang are caught and
    degrade to empty results for that language."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", MagicMock(return_value=client)),
        patch.object(msa, "_cache_set"),
    ):
        candidates = await msa._search_tmdb("Timeout")

    assert candidates == []


async def test_search_tmdb_language_http_status_error_returns_empty():
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.HTTPStatusError("bad", request=MagicMock(), response=MagicMock()))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", MagicMock(return_value=client)),
        patch.object(msa, "_cache_set"),
    ):
        candidates = await msa._search_tmdb("Http")

    assert candidates == []


async def test_search_tmdb_en_us_provides_original_title():
    """When the en-US pass carries the original title and zh-CN does not, the
    merged entry picks it up (covers the en-US original_title branch)."""
    zh = {
        "results": [
            {
                "media_type": "movie", "id": 3,
                "title": "电影", "release_date": "2018-02-02",
                "vote_average": 6.0,
            }
        ]
    }
    en = {
        "results": [
            {
                "media_type": "movie", "id": 3,
                "title": "Movie", "original_title": "Native Name",
                "release_date": "2018-02-02",
            }
        ]
    }
    with (
        patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", _httpx_client_mock({"zh-CN": zh, "en-US": en})),
        patch.object(msa, "_tmdb_image_base", return_value="https://img/"),
        patch.object(msa, "_tmdb_genre_map", return_value={}),
    ):
        candidates = await msa._search_tmdb("Native")

    assert len(candidates) == 1
    assert candidates[0]["original_title"] == "Native Name"
    assert candidates[0]["title_cn"] == "电影"
    assert candidates[0]["title_en"] == "Movie"


async def test_search_tmdb_empty_results_cached():
    client = _sync_async_client_for({})
    with (
        patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", MagicMock(return_value=client)),
        patch.object(msa, "_cache_set") as cache_set,
    ):
        candidates = await msa._search_tmdb("Empty")
    assert candidates == []
    cache_set.assert_called_once()


async def test_search_tmdb_filters_invalid_media_types_and_ids():
    results = {
        "multi": {
            "results": [
                {"media_type": "person", "id": 1},
                {"media_type": "tv", "id": None},
                {"media_type": "tv", "id": 5, "name": "Good", "first_air_date": "2020"},
            ]
        }
    }
    with (
        patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", _httpx_client_mock(results)),
        patch.object(msa, "_tmdb_image_base", return_value="https://img/"),
        patch.object(msa, "_tmdb_genre_map", return_value={}),
    ):
        candidates = await msa._search_tmdb("Mixed")

    assert [c["external_id"] for c in candidates] == ["tmdb:5"]


async def test_search_tmdb_drops_invalid_candidate_via_validate():
    # A tv item with no name/title/original_title → fails _validate_candidate.
    results = {
        "multi": {
            "results": [
                {"media_type": "tv", "id": 9, "overview": "o", "first_air_date": "2020-01-01"},
                {"media_type": "tv", "id": 10, "name": "Real Show", "first_air_date": "2021-05-05"},
            ]
        }
    }
    with (
        patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", _httpx_client_mock(results)),
        patch.object(msa, "_tmdb_image_base", return_value="https://img/"),
        patch.object(msa, "_tmdb_genre_map", return_value={}),
    ):
        candidates = await msa._search_tmdb("Filtered")

    assert [c["external_id"] for c in candidates] == ["tmdb:10"]


def _sync_async_client_for(json_results):
    """Sync client returning a fixed JSON payload regardless of URL."""
    client = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_results
    client.get.return_value = resp
    client.__aenter__ = MagicMock(return_value=client)
    client.__aexit__ = MagicMock(return_value=False)
    return client


async def test_search_tmdb_task_exception_become_empty():
    import asyncio

    # A BaseException (CancelledError) escapes _search_lang's except Exception
    # handler and surfaces through gather(return_exceptions=True).
    client = MagicMock()
    client.get = AsyncMock(side_effect=asyncio.CancelledError("cancelled"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.dict(_OVERRIDES, {"tmdb_api_key": "k"}),
        patch("httpx.AsyncClient", MagicMock(return_value=client)),
        patch.object(msa, "_cache_set") as cache_set,
    ):
        candidates = await msa._search_tmdb("Explode")

    assert candidates == []
    cache_set.assert_called_once_with("tmdb", "Explode", [])


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------


def test_validate_candidate():
    assert msa._validate_candidate({"content_type": "tv", "title_cn": "X"}) is True
    assert msa._validate_candidate({"content_type": "tv", "title_en": "X"}) is True
    assert msa._validate_candidate({"content_type": "tv", "original_title": "X"}) is True
    assert msa._validate_candidate({"content_type": "tv"}) is False
    assert msa._validate_candidate({"title_cn": "X"}) is False
    assert msa._validate_candidate({"content_type": "audio", "title_cn": "X"}) is False
