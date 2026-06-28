"""Unit tests for metadata_search_agent: TMDB, Exa sources."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.metadata_search_agent import (
    MetadataCandidate,
    _validate_candidate,
    _cache_get,
    _cache_set,
    _cache_key,
    _parse_year,
    _fmt_date,
    _tmdb_poster_url,
    _validate_poster_url,
    search_metadata,
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
    monkeypatch.setattr("app.services.metadata_search_agent.settings.tmdb_api_key", "test_key")
    monkeypatch.setattr("app.services.metadata_search_agent._tmdb_image_base", lambda key: "https://image.tmdb.org/t/p/")
    from app.services.metadata_search_agent import _search_tmdb, _cache
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
    monkeypatch.setattr("app.services.metadata_search_agent.settings.tmdb_api_key", "test_key")
    monkeypatch.setattr("app.services.metadata_search_agent._tmdb_image_base", lambda key: "https://image.tmdb.org/t/p/")
    from app.services.metadata_search_agent import _search_tmdb, _cache
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
async def test_tmdb_no_api_key_returns_empty():
    from app.services.metadata_search_agent import _search_tmdb, _cache
    _cache.clear()
    # Settings.tmdb_api_key is "" by default
    # We need to ensure it's unset
    import app.services.metadata_search_agent as agent_mod
    agent_mod.settings.tmdb_api_key = ""
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


@pytest.mark.asyncio
async def test_exa_search_returns_result(monkeypatch):
    """Test Exa Agent search returns structured metadata."""
    monkeypatch.setattr("app.services.metadata_search_agent.settings.exa_api_key", "test_key")
    from app.services.metadata_search_agent import _search_exa, _cache
    _cache.clear()

    import app.services.metadata_search_agent as agent_mod

    # Mock AsyncExa
    class MockRun:
        id = "run-123"

    class MockOutput:
        structured = EXA_STRUCTURED_RESULT

    class MockResult:
        status = "completed"
        output = MockOutput()

    class MockExaAgentRuns:
        async def create(self, query, output_schema, effort):
            return MockRun()
        async def poll_until_finished(self, run_id, **kwargs):
            return MockResult()

    class MockExaAgent:
        def __init__(self):
            self.runs = MockExaAgentRuns()

    class MockExa:
        def __init__(self, api_key=None):
            self.agent = MockExaAgent()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("exa_py.AsyncExa", MockExa)
    # Also mock poster validation to pass through
    async def _mock_validate(url, **kw):
        return url
    monkeypatch.setattr(agent_mod, "_validate_poster_url", _mock_validate)

    results = await _search_exa("Breaking Bad")
    assert len(results) == 1
    r = results[0]
    assert r["content_type"] == "tv"
    assert r["title_en"] == "Breaking Bad"
    assert r["title_cn"] == "绝命毒师"
    assert r["external_source"] == "exa"
    assert r["external_id"] == "tt0903747"


@pytest.mark.asyncio
async def test_exa_no_api_key_returns_empty():
    """Exa returns empty list when no API key configured."""
    from app.services.metadata_search_agent import _search_exa, _cache
    _cache.clear()
    import app.services.metadata_search_agent as agent_mod
    agent_mod.settings.exa_api_key = ""
    results = await _search_exa("Breaking Bad")
    assert results == []


@pytest.mark.asyncio
async def test_exa_failed_run_returns_empty(monkeypatch):
    """Failed Exa run returns empty list gracefully."""
    monkeypatch.setattr("app.services.metadata_search_agent.settings.exa_api_key", "test_key")
    from app.services.metadata_search_agent import _search_exa, _cache
    _cache.clear()

    class MockRun:
        id = "run-fail"

    class MockResult:
        status = "failed"
        output = None
        stop_reason = "error"

    class MockExaAgentRuns:
        async def create(self, *a, **kw):
            return MockRun()
        async def poll_until_finished(self, *a, **kw):
            return MockResult()

    class MockExaAgent:
        def __init__(self):
            self.runs = MockExaAgentRuns()

    class MockExa:
        def __init__(self, api_key=None):
            self.agent = MockExaAgent()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("exa_py.AsyncExa", MockExa)
    results = await _search_exa("Unknown Title")
    assert results == []


@pytest.mark.asyncio
async def test_validate_poster_url():
    """Test poster URL validation (sync helper on known URLs)."""
    # None should return None
    assert await _validate_poster_url(None) is None
    assert await _validate_poster_url("") is None


# ---------------------------------------------------------------------------
# Full agent: search_metadata orchestration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_metadata_empty_title_returns_empty():
    assert await search_metadata("") == []
    assert await search_metadata("   ") == []


@pytest.mark.asyncio
async def test_search_metadata_all_sources_fail_returns_empty(monkeypatch):
    """When no API keys are set, all sources fail gracefully."""
    import app.services.metadata_search_agent as agent_mod
    agent_mod.settings.tmdb_api_key = ""
    agent_mod.settings.exa_api_key = ""
    agent_mod.settings.llm_api_key = ""

    results = await search_metadata("Breaking Bad")
    assert results == []


@pytest.mark.asyncio
async def test_search_metadata_handles_source_exception(monkeypatch):
    """When TMDB throws, agent continues to other sources."""
    import app.services.metadata_search_agent as agent_mod
    agent_mod.settings.tmdb_api_key = "k"
    agent_mod.settings.exa_api_key = ""
    agent_mod.settings.llm_api_key = ""

    agent_mod._cache.clear()

    # Make TMDB throw
    async def broken_tmdb(title):
        raise RuntimeError("TMDB down")
    monkeypatch.setattr(agent_mod, "_search_tmdb", broken_tmdb)

    results = await search_metadata("Breaking Bad")
    assert results == []  # No source succeeded


@pytest.mark.asyncio
async def test_search_metadata_returns_empty_when_no_llm_fallback(monkeypatch):
    """When all structured sources fail and no LLM fallback exists, returns empty."""
    import app.services.metadata_search_agent as agent_mod
    agent_mod.settings.tmdb_api_key = ""
    agent_mod.settings.exa_api_key = ""
    agent_mod._cache.clear()

    results = await search_metadata("Unknown Obscure Title")
    assert results == []
