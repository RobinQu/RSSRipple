"""Unit tests for metadata_search_agent: TMDB, Exa sources."""

from __future__ import annotations

import pytest

from app.services.metadata_search_agent import (
    _cache_get,
    _cache_key,
    _cache_set,
    _extract_exa_candidates,
    _extract_exa_structured,
    _fmt_date,
    _normalize_exa_candidate,
    _parse_year,
    _tmdb_poster_url,
    _validate_candidate,
)

# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------


def test_validate_candidate_pass_with_minimal_fields():
    assert _validate_candidate({"content_type": "tv", "title_en": "Breaking Bad"})


def test_validate_candidate_pass_with_title_cn_only():
    assert _validate_candidate({"content_type": "movie", "title_cn": "盗梦空间"})


def test_validate_candidate_fail_missing_title():
    assert not _validate_candidate({"content_type": "tv", "title_cn": None, "title_en": None, "original_title": None})


def test_validate_candidate_fail_missing_content_type():
    assert not _validate_candidate({"title_en": "Show", "content_type": None})


def test_validate_candidate_fail_bad_content_type():
    assert not _validate_candidate({"title_en": "Show", "content_type": "person"})


# ---------------------------------------------------------------------------
# Session cache
# ---------------------------------------------------------------------------


def test_cache_get_set():
    _cache_set("tmdb", "Breaking Bad", [{"title_en": "Breaking Bad"}])
    result = _cache_get("tmdb", "Breaking Bad")
    assert result is not None
    assert result[0]["title_en"] == "Breaking Bad"


def test_cache_key_normalizes():
    assert _cache_key("tmdb", " Breaking Bad ") == _cache_key("tmdb", "breaking bad")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_parse_year():
    assert _parse_year("2008") == 2008
    assert _parse_year("2008-01-20") == 2008
    assert _parse_year(None) is None
    assert _parse_year("") is None
    assert _parse_year(2008) == 2008


def test_fmt_date():
    assert _fmt_date("2008-01-20") == "2008-01-20"
    assert _fmt_date("2008") == "2008-01-01"
    assert _fmt_date(None) is None


def test_tmdb_poster_url():
    base = "https://image.tmdb.org/t/p/"
    assert _tmdb_poster_url("/abc.jpg", base) == "https://image.tmdb.org/t/p/w500/abc.jpg"
    assert _tmdb_poster_url(None) is None
    assert _tmdb_poster_url("") is None


def test_extract_exa_candidates_from_candidates_array():
    structured = {
        "candidates": [
            {"content_type": "tv", "title_en": "Breaking Bad"},
            {"content_type": "movie", "title_en": "El Camino"},
        ],
        "reason": "two matches",
    }
    assert _extract_exa_candidates(structured) == structured["candidates"]


def test_extract_exa_candidates_supports_legacy_single_candidate():
    structured = {"content_type": "tv", "title_en": "Breaking Bad"}
    assert _extract_exa_candidates(structured) == [structured]


def test_extract_exa_structured_from_sdk_like_model():
    class Output:
        structured = {"candidates": [{"content_type": "tv", "title_en": "Breaking Bad"}]}

    class Run:
        output = Output()

    assert _extract_exa_structured(Run()) == {
        "candidates": [{"content_type": "tv", "title_en": "Breaking Bad"}]
    }


def test_normalize_exa_candidate_adds_external_fields_and_type_alias():
    candidate = _normalize_exa_candidate(
        {"content_type": "anime", "title_en": "Frieren", "genre": None},
        "[Subs] Frieren - 01",
        0,
    )
    assert candidate["content_type"] == "tv"
    assert candidate["external_source"] == "exa"
    assert candidate["external_id"].startswith("exa:")
    assert candidate["genre"] == []


# ---------------------------------------------------------------------------
# TMDB source (mocked httpx)
# ---------------------------------------------------------------------------

TMDB_TV_RESPONSE_ZH = {
    "results": [
        {
            "id": 1396,
            "media_type": "tv",
            "name": "绝命毒师",
            "original_name": "Breaking Bad",
            "overview": "一位高中化学老师...",
            "poster_path": "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg",
            "vote_average": 8.913,
            "genre_ids": [18],
            "first_air_date": "2008-01-20",
        }
    ]
}

TMDB_TV_RESPONSE_EN = {
    "results": [
        {
            "id": 1396,
            "media_type": "tv",
            "name": "Breaking Bad",
            "original_name": "Breaking Bad",
            "overview": "A chemistry teacher diagnosed with cancer...",
            "poster_path": "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg",
            "vote_average": 8.9,
            "genre_ids": [18, 80],
            "first_air_date": "2008-01-20",
        }
    ]
}

TMDB_MOVIE_RESPONSE_EN = {
    "results": [
        {
            "id": 27205,
            "media_type": "movie",
            "title": "Inception",
            "original_title": "Inception",
            "overview": "A thief who steals corporate secrets...",
            "poster_path": "/ljsZTbVsrQSqZgWeep2B1QiDKuh.jpg",
            "vote_average": 8.369,
            "genre_ids": [28, 878, 12],
            "release_date": "2010-07-15",
        }
    ]
}

TMDB_MIXED_RESPONSE = {
    "results": [
        {
            "id": 1396,
            "media_type": "tv",
            "name": "Breaking Bad",
            "original_name": "Breaking Bad",
            "overview": "TV show about chemistry teacher",
            "poster_path": "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg",
            "vote_average": 8.9,
            "genre_ids": [18],
            "first_air_date": "2008-01-20",
        },
        {
            "id": 99999,
            "media_type": "person",  # should be filtered out
            "name": "Bryan Cranston",
        },
    ]
}

CONFIG_RESPONSE = {
    "images": {
        "secure_base_url": "https://image.tmdb.org/t/p/",
    }
}


def _make_mock_async_client(responses: dict[str, dict]):
    """Create a mock httpx.AsyncClient that returns canned responses by URL path."""

    class MockResponse:
        def __init__(self, data):
            self._data = data
            self.status_code = 200

        def json(self):
            return self._data

        def raise_for_status(self):
            pass

    class MockClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            # Match by endpoint path
            for path_key, resp_data in responses.items():
                if path_key in url:
                    return MockResponse(resp_data)
            return MockResponse({"results": []})

    return MockClient


@pytest.mark.asyncio
async def test_tmdb_search_returns_merged_results(monkeypatch):
    """Test that zh-CN + en-US results are merged by TMDB ID."""
    monkeypatch.setattr("app.config.settings.tmdb_api_key", "test_key")
    monkeypatch.setattr("app.services.metadata_search_agent._tmdb_image_base", lambda key: "https://image.tmdb.org/t/p/")
    from app.services.metadata_search_agent import _cache, _search_tmdb
    _cache.clear()

    import httpx as httpx_mod

    class FakeResponse:
        def __init__(self, data):
            self._data = data
            self.status_code = 200

        def json(self):
            return self._data

        def raise_for_status(self):
            pass

    async def fake_get(self, url, **kwargs):
        params = kwargs.get("params", {})
        lang = params.get("language", "en-US")
        if "configuration" in url:
            return FakeResponse(CONFIG_RESPONSE)
        elif lang == "zh-CN":
            return FakeResponse(TMDB_TV_RESPONSE_ZH)
        else:
            return FakeResponse(TMDB_TV_RESPONSE_EN)

    # Create a mock AsyncClient that supports async context manager
    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        get = fake_get  # instance method

    monkeypatch.setattr(httpx_mod, "AsyncClient", MockAsyncClient)

    results = await _search_tmdb("Breaking Bad")
    assert len(results) == 1
    r = results[0]
    assert r["content_type"] == "tv"
    assert r["title_cn"] == "绝命毒师"  # from zh-CN
    assert r["title_en"] == "Breaking Bad"  # from en-US
    assert r["external_id"] == "tmdb:1396"
    assert r["external_source"] == "tmdb"
    assert r["year"] == 2008
    assert r["rating"] == 8.913
    assert r["poster_url"] == "https://image.tmdb.org/t/p/w500/ggFHVNu6YYI5L9pCfOacjizRGt.jpg"


@pytest.mark.asyncio
async def test_tmdb_filters_out_person_results(monkeypatch):
    """Test that 'person' media_type is filtered out."""
    monkeypatch.setattr("app.config.settings.tmdb_api_key", "test_key")
    monkeypatch.setattr("app.services.metadata_search_agent._tmdb_image_base", lambda key: "https://image.tmdb.org/t/p/")
    from app.services.metadata_search_agent import _cache, _search_tmdb
    _cache.clear()

    import httpx as httpx_mod

    class FakeResponse:
        def __init__(self, data):
            self._data = data
            self.status_code = 200
        def json(self):
            return self._data
        def raise_for_status(self):
            pass

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            if "configuration" in url:
                return FakeResponse(CONFIG_RESPONSE)
            return FakeResponse(TMDB_MIXED_RESPONSE)

    monkeypatch.setattr(httpx_mod, "AsyncClient", MockAsyncClient)
    results = await _search_tmdb("Breaking Bad")
    # Only 1 result (the TV one), person is filtered
    assert len(results) == 1
    assert results[0]["content_type"] == "tv"


@pytest.mark.asyncio
async def test_tmdb_no_api_key_returns_empty(monkeypatch):
    from app.services.metadata_search_agent import _cache, _search_tmdb
    _cache.clear()
    # runtime_config falls back to the shared settings instance; ensure no key.
    monkeypatch.setattr("app.config.settings.tmdb_api_key", "")
    results = await _search_tmdb("Breaking Bad")
    assert results == []


# ---------------------------------------------------------------------------
# Exa AI Agent source (mocked exa_py)
# ---------------------------------------------------------------------------

EXA_STRUCTURED_RESULT = {
    "content_type": "tv",
    "title_cn": "绝命毒师",
    "title_en": "Breaking Bad",
    "original_title": "Breaking Bad",
    "description": "A high school chemistry teacher diagnosed with cancer...",
    "poster_url": "https://image.tmdb.org/t/p/w500/ggFHVNu6YYI5L9pCfOacjizRGt.jpg",
    "year": 2008,
    "rating": 8.9,
    "genre": ["Drama", "Crime"],
    "status": "Ended",
    "external_id": "tt0903747",
    "number_of_episodes": 62,
    "number_of_seasons": 5,
    "start_date": "2008-01-20",
    "end_date": "2013-09-29",
    "release_date": None,
    "runtime": None,
}

# NOTE: search_metadata() is now only a single-source compatibility dispatcher.
# Exa/TMDB/Wikipedia orchestration lives in UnifiedMetadataAgent
# (app/services/metadata_agent.py). See tests/unit/test_metadata_agent.py for
# source-restricted agent tests.


# ---------------------------------------------------------------------------
# Jina Search + Reader source (mocked httpx)
# ---------------------------------------------------------------------------

JINA_SEARCH_RESPONSE = {
    "code": 200,
    "status": 200,
    "data": [
        {
            "title": "Breaking Bad — Wikipedia",
            "url": "https://en.wikipedia.org/wiki/Breaking_Bad",
            "description": "American crime drama TV series",
            "content": "# Breaking Bad\n\nBreaking Bad is an American crime drama...",
        },
        {
            "title": "Breaking Bad (TV Series 2008–2013) — IMDb",
            "url": "https://www.imdb.com/title/tt0903747/",
            "description": "Created by Vince Gilligan.",
            "content": "# Breaking Bad\n\nIMDb Rating: 9.5",
        },
        {
            "title": "Breaking Bad — Fandom",
            "url": "https://breakingbad.fandom.com/wiki/Breaking_Bad",
            "description": "Fandom wiki.",
            "content": "# Breaking Bad Wiki",
        },
        {
            "title": "Breaking Bad Season 1 — Fandom",
            "url": "https://breakingbad.fandom.com/wiki/Season_1",
            "description": "Season 1.",
            "content": "# Season 1",
        },
        {
            "title": "Breaking Bad Season 2 — Fandom",
            "url": "https://breakingbad.fandom.com/wiki/Season_2",
            "description": "Season 2.",
            "content": "# Season 2",
        },
    ],
}

JINA_READER_RESPONSE = {
    "code": 200,
    "status": 200,
    "data": {
        "title": "Breaking Bad",
        "url": "https://www.imdb.com/title/tt0903747/",
        "description": "IMDb page",
        "content": "# Breaking Bad\n\nRating: 9.5",
        "links": [{"url": "https://www.imdb.com/title/tt0903747/fullcredits", "text": "full cast"}],
    },
}


class _JinaCaptured:
    """Records the last POST call's url/headers/json for assertions."""

    def __init__(self) -> None:
        self.url: str | None = None
        self.headers: dict | None = None
        self.json: dict | None = None


def _make_jina_post_client(response_body, captured: _JinaCaptured):
    class FakeResponse:
        def __init__(self, body):
            self._body = body
            self.status_code = 200

        def json(self):
            return self._body

        def raise_for_status(self):
            pass

    class MockClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, headers=None, json=None, **kwargs):
            captured.url = url
            captured.headers = headers or {}
            captured.json = json
            return FakeResponse(response_body)

    return MockClient


@pytest.mark.asyncio
async def test_jina_search_parses_envelope_and_caps_per_hostname(monkeypatch):
    monkeypatch.setattr("app.config.settings.jina_api_key", "jina_test_key")
    from app.services.metadata_search_agent import _cache, _search_jina

    _cache.clear()

    import httpx as httpx_mod

    captured = _JinaCaptured()
    monkeypatch.setattr(httpx_mod, "AsyncClient", _make_jina_post_client(JINA_SEARCH_RESPONSE, captured))

    results = await _search_jina("Breaking Bad")

    # 5 raw hits: wiki + imdb + 3 fandom. keep_k_per_hostname(k=2) drops the
    # 3rd fandom entry → 4 results, fandom capped at 2.
    assert len(results) == 4
    assert results[0]["url"] == "https://en.wikipedia.org/wiki/Breaking_Bad"
    assert results[1]["url"] == "https://www.imdb.com/title/tt0903747/"
    fandom = [r for r in results if "fandom.com" in r["url"]]
    assert len(fandom) == 2

    # HTTP call shape
    assert captured.url == "https://s.jina.ai/"
    assert captured.headers["Authorization"] == "Bearer jina_test_key"
    assert captured.headers["X-Preset"] == "agent"
    assert captured.headers["X-Engine"] == "auto"
    assert captured.headers["Accept"] == "application/json"
    assert captured.json == {"q": "Breaking Bad", "num": 3, "gl": "us", "hl": "en"}


@pytest.mark.asyncio
async def test_jina_search_cache_hit_skips_http(monkeypatch):
    monkeypatch.setattr("app.config.settings.jina_api_key", "jina_test_key")
    from app.services.metadata_search_agent import _cache, _cache_set, _search_jina

    _cache.clear()
    _cache_set("jina", "Breaking Bad", [{"title": "cached", "url": "https://example.com", "description": None, "content": "x"}])

    import httpx as httpx_mod

    class MockClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **k):
            raise AssertionError("HTTP should not be called on cache hit")

    monkeypatch.setattr(httpx_mod, "AsyncClient", MockClient)

    results = await _search_jina("Breaking Bad")
    assert len(results) == 1
    assert results[0]["title"] == "cached"


@pytest.mark.asyncio
async def test_jina_search_empty_data_returns_empty(monkeypatch):
    monkeypatch.setattr("app.config.settings.jina_api_key", "jina_test_key")
    from app.services.metadata_search_agent import _cache, _search_jina

    _cache.clear()

    import httpx as httpx_mod

    captured = _JinaCaptured()
    monkeypatch.setattr(
        httpx_mod,
        "AsyncClient",
        _make_jina_post_client({"code": 200, "status": 200, "data": []}, captured),
    )

    results = await _search_jina("Nothing Matches")
    assert results == []


@pytest.mark.asyncio
async def test_jina_search_no_api_key_returns_empty(monkeypatch):
    monkeypatch.setattr("app.config.settings.jina_api_key", "")
    from app.services.metadata_search_agent import _cache, _search_jina

    _cache.clear()
    results = await _search_jina("Breaking Bad")
    assert results == []


@pytest.mark.asyncio
async def test_jina_read_parses_envelope(monkeypatch):
    monkeypatch.setattr("app.config.settings.jina_api_key", "jina_test_key")
    import httpx as httpx_mod

    from app.services.metadata_search_agent import _read_jina_url

    captured = _JinaCaptured()
    monkeypatch.setattr(httpx_mod, "AsyncClient", _make_jina_post_client(JINA_READER_RESPONSE, captured))

    data = await _read_jina_url("https://www.imdb.com/title/tt0903747/")
    assert data["title"] == "Breaking Bad"
    assert data["url"] == "https://www.imdb.com/title/tt0903747/"
    assert "Rating: 9.5" in data["content"]
    # with_links defaults to False → no links summary requested/returned
    assert data["links"] is None

    assert captured.url == "https://r.jina.ai/"
    assert captured.headers["Authorization"] == "Bearer jina_test_key"
    assert "X-With-Links-Summary" not in captured.headers
    assert captured.json == {"url": "https://www.imdb.com/title/tt0903747/"}


@pytest.mark.asyncio
async def test_jina_read_with_links_sends_header(monkeypatch):
    monkeypatch.setattr("app.config.settings.jina_api_key", "jina_test_key")
    import httpx as httpx_mod

    from app.services.metadata_search_agent import _read_jina_url

    captured = _JinaCaptured()
    monkeypatch.setattr(httpx_mod, "AsyncClient", _make_jina_post_client(JINA_READER_RESPONSE, captured))

    data = await _read_jina_url("https://www.imdb.com/title/tt0903747/", with_links=True)
    assert data["links"] == [{"url": "https://www.imdb.com/title/tt0903747/fullcredits", "text": "full cast"}]
    assert captured.headers["X-With-Links-Summary"] == "true"


@pytest.mark.asyncio
async def test_jina_read_no_api_key_returns_empty(monkeypatch):
    monkeypatch.setattr("app.config.settings.jina_api_key", "")
    from app.services.metadata_search_agent import _read_jina_url

    data = await _read_jina_url("https://example.com")
    assert data == {}
