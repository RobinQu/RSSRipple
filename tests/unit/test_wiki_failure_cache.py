"""Recorded title; source timeout and model no-match are synthetic faults."""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.file_resource import FileResource
from app.models.metadata_cache import MetadataCache
from app.services.metadata_agent import UnifiedMetadataAgent
from tests.metadata_corpus.dataset import load_corpus


@pytest.mark.parametrize("partial_failure,web_negative", [
    (False, False), (True, False), (False, True), (True, True),
], ids=["all_failed_no_web", "partial_failed_no_web", "all_failed_empty_web", "partial_failed_empty_web"])
async def test_total_wiki_failure_does_not_poison_cache(
    db_session, sample_channel, monkeypatch, partial_failure, web_negative,
):
    case = next(c for c in load_corpus()[1]["cases"]
                if c["id"] == "011c6d44-68cf-43a8-bad3-f0398ce20a95")
    sample_channel.metadata_source = "wikipedia"
    sample_channel.metadata_fallback_sources = ["wikipedia"] if web_negative else []
    resource = FileResource(channel_id=sample_channel.id, guid=str(uuid.uuid4()),
                            title_raw=case["input"]["title_raw"], torrent_url="https://unused.invalid/file")
    db_session.add(resource)
    await db_session.commit()
    source = AsyncMock(return_value={"success": False, "error": "Wikipedia request failed: timeout"})
    if partial_failure:
        async def search(query, language):
            return ({"success": True, "data": []} if language == "en" else
                    {"success": False, "error": "Wikipedia request failed: timeout"})
        source.side_effect = search
    monkeypatch.setattr("app.services.metadata_wiki_judge._execute_search_wikipedia", source)
    fallback = AsyncMock(return_value=(
        {"found": False, "reason": "No matching work found", "content_type": "tv"},
        {"error": None, "source_errors": {}},
    ) if web_negative else None)
    monkeypatch.setattr("app.services.metadata_wiki_judge.web_fallback_judge", fallback)
    agent = UnifiedMetadataAgent()
    agent._model = AsyncMock()
    agent._model.ainvoke.return_value = SimpleNamespace(content=json.dumps({
        "found": False, "reason": "No matching work found", "content_type": "tv",
    }))
    result = await agent.process(resource, sample_channel, db_session)
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with factory() as observer:
        cached = (await observer.execute(select(MetadataCache))).scalars().all()
        stored = await observer.get(FileResource, resource.id)
        assert not cached, "source lookup was incomplete but negative cache was persisted"
        assert stored.metadata_failure_type == "transient"
    assert result.search_error
    before = source.await_count
    # Recovery to a successful empty response must actually query the source.
    source.side_effect = None
    source.return_value = {"success": True, "data": []}
    await agent.process(resource, sample_channel, db_session)
    await db_session.commit()
    assert source.await_count > before
    async with factory() as observer:
        assert len((await observer.execute(select(MetadataCache))).scalars().all()) == 1


@pytest.mark.parametrize("found", [False, True])
async def test_web_fallback_preserves_primary_failure_or_success(monkeypatch, found):
    from app.services.metadata_agent import ResourceMetadata, _classify_failure
    from app.services.metadata_wiki_judge import run_search_then_judge

    monkeypatch.setattr("app.services.metadata_wiki_judge._candidate_queries",
                        lambda *_: [("Synthetic query", "en")])
    monkeypatch.setattr("app.services.metadata_wiki_judge._execute_search_wikipedia", AsyncMock(return_value={
        "success": False, "error": "Wikipedia request failed: timeout",
    }))
    fallback = {"found": found, "reason": "Synthetic fallback result"}
    if found:
        fallback["matched_entity"] = {"external_source": "wikipedia", "external_id": "wikipedia:en:90007005"}
    monkeypatch.setattr("app.services.metadata_wiki_judge.web_fallback_judge",
                        AsyncMock(return_value=(fallback, {"error": None})))
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content=json.dumps({"found": False, "reason": "No match"}))
    react = AsyncMock()
    verdict, info = await run_search_then_judge(model, "Synthetic query", react_runner=react,
                                               msg_builder=lambda *_: "synthetic request")
    assert verdict["found"] is found
    assert "wikipedia:en" in info["source_errors"]
    assert _classify_failure(ResourceMetadata(**verdict, search_error=info["error"])) == (None if found else "transient")
    react.assert_not_awaited()


@pytest.mark.parametrize("recovered", [False, True])
async def test_failed_page_lookup_preserves_retry_outcome(monkeypatch, recovered):
    from app.services.metadata_agent import ResourceMetadata, _classify_failure
    from app.services.metadata_wiki_judge import run_search_then_judge

    monkeypatch.setattr("app.services.metadata_wiki_judge._candidate_queries", lambda *_: [("Recorded query", "en")])
    monkeypatch.setattr("app.services.metadata_wiki_judge._execute_search_wikipedia", AsyncMock(return_value={
        "success": True, "data": [{"title": "Synthetic candidate", "page_id": 90007001}],
    }))
    monkeypatch.setattr("app.services.metadata_wiki_judge._execute_get_wikipedia_page", AsyncMock(return_value={
        "success": False, "data": {}, "error": "Wikipedia request failed: timeout",
    }))
    monkeypatch.setattr("app.services.metadata_wiki_judge.web_fallback_judge", AsyncMock(return_value=None))
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content=json.dumps({"found": False, "reason": "No match"}))
    retry = {"found": recovered, "clean_title": "Synthetic fault query", "reason": "No match"}
    if recovered:
        retry["matched_entity"] = {"external_source": "wikipedia", "external_id": "wikipedia:en:90007001"}
    react = AsyncMock(return_value=(retry, {"error": None}))
    verdict, info = await run_search_then_judge(model, "Synthetic fault query", react_runner=react,
                                               msg_builder=lambda *args: "synthetic message")
    assert _classify_failure(ResourceMetadata(**verdict, search_error=info.get("error"))) == (None if recovered else "transient")
    assert "page:en" in info["source_errors"]


@pytest.mark.parametrize("failed_languages", [{"zh-CN", "en-US"}, {"zh-CN"}])
async def test_tmdb_failed_search_is_not_negative_cached(monkeypatch, failed_languages):
    import httpx

    from app.services import metadata_search_agent as msa

    case = next(c for c in load_corpus()[1]["cases"]
                if c["id"] == "011c6d44-68cf-43a8-bad3-f0398ce20a95")
    title = case["input"]["title_raw"]
    monkeypatch.setattr(msa, "_cache", {})
    monkeypatch.setattr("app.services.runtime_config._overrides", {"tmdb_api_key": "synthetic"})
    calls = []

    async def get(self, url, **kwargs):
        language = kwargs["params"]["language"]
        calls.append(language)
        if language in failed_languages:
            raise httpx.ReadTimeout("synthetic timeout")
        return httpx.Response(200, json={"results": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    from app.services.metadata_source_io import _execute_search_tmdb

    failed = await _execute_search_tmdb(title)
    assert failed["success"] is False
    assert "request failed" in failed["error"]
    assert msa._cache_get("tmdb", title) is None
    failed_languages = set()
    assert await msa._search_tmdb(title) == []
    assert len(calls) == 4
    assert msa._cache_get("tmdb", title) == []


async def test_tmdb_partial_success_retains_candidate_without_caching(monkeypatch):
    import httpx

    from app.services import metadata_search_agent as msa
    from app.services.metadata_source_io import _execute_search_tmdb

    monkeypatch.setattr(msa, "_cache", {})
    monkeypatch.setattr("app.services.runtime_config._overrides", {"tmdb_api_key": "synthetic"})
    monkeypatch.setattr(msa, "_tmdb_image_base", lambda *_: "https://unused.invalid/")
    monkeypatch.setattr(msa, "_tmdb_genre_map", lambda *_: {})

    async def get(self, url, **kwargs):
        if kwargs["params"]["language"] == "zh-CN":
            raise httpx.ReadTimeout("synthetic timeout")
        return httpx.Response(200, json={"results": [{
            "media_type": "movie", "id": 90007002, "title": "Synthetic success",
        }]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    result = await _execute_search_tmdb("Synthetic success")
    assert result["success"] is True
    assert [item["external_id"] for item in result["data"]] == ["tmdb:90007002"]
    assert msa._cache_get("tmdb", "Synthetic success") is None


async def test_wiki_partial_failure_keeps_grounded_success(monkeypatch):
    from app.services.metadata_agent import ResourceMetadata, _classify_failure
    from app.services.metadata_wiki_judge import run_search_then_judge

    monkeypatch.setattr("app.services.metadata_wiki_judge._candidate_queries",
                        lambda *_: [("Recorded query", "zh"), ("Recorded query", "en")])
    async def search(query, language):
        if language == "zh":
            return {"success": False, "error": "Wikipedia request failed: timeout"}
        return {"success": True, "data": [{"title": "Synthetic movie", "page_id": 90007003}]}
    monkeypatch.setattr("app.services.metadata_wiki_judge._execute_search_wikipedia", search)
    monkeypatch.setattr("app.services.metadata_wiki_judge._execute_get_wikipedia_page", AsyncMock(return_value={
        "success": True, "data": {"title": "Synthetic movie", "categories": ["2024 films"]},
    }))
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content=json.dumps({
        "found": True, "content_type": "movie", "clean_title": "Synthetic movie",
        "matched_entity": {"external_source": "wikipedia", "external_id": "wikipedia:en:90007003"},
    }))
    verdict, info = await run_search_then_judge(model, "Synthetic fault query", react_runner=AsyncMock(),
                                               msg_builder=lambda *args: "synthetic message")
    assert verdict["found"] is True
    assert verdict["matched_entity"]["external_id"] == "wikipedia:en:90007003"
    assert "wikipedia:zh" in info["source_errors"]
    assert info["error"] is None
    assert _classify_failure(ResourceMetadata(**verdict, search_error=info["error"])) is None
