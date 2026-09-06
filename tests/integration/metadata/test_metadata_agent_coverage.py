"""UnifiedMetadataAgent integration coverage (``metadata_agent``).

Exercises the agent's tool wrappers, finalize/search-info extractors, genre
fallback, TMDB episode attach, message builders, web-fallback routing and the
``process`` pipeline branches with every external surface (LLM, HTTP, DB
where avoidable) stubbed — no network, no LLM.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import app.services.metadata_service as metadata_service
from app.services import metadata_agent as ma
from app.services.metadata_agent import ResourceMetadata, UnifiedMetadataAgent
from tests.unit import conftest as _unit_conftest

db_session = _unit_conftest.db_session
sample_channel = _unit_conftest.sample_channel


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch):
    monkeypatch.setattr("app.services.runtime_config._overrides", {
        "llm_api_key": "fake-key",
        "llm_model": "fake-model",
        "llm_base_url": "http://llm.invalid/v1",
        "llm_enable_thinking": "0",
    })


def _agent() -> UnifiedMetadataAgent:
    return UnifiedMetadataAgent()


def _ns_resource(**over):
    base = dict(
        title_raw="[G] Show - 01 [1080p]", series_id=None, movie_id=None,
        metadata_attempts=0, last_metadata_attempt_at=None,
        metadata_failure_type=None, search_title="Show",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ns_channel(**over):
    base = dict(id="ch", name="CH", metadata_source=None, metadata_fallback_sources=None)
    base.update(over)
    return SimpleNamespace(**base)


def _found_info(method="search_then_judge"):
    return {"method": method, "data_sources_used": ["wikipedia"],
            "source_errors": {}, "error": None}


# ---------------------------------------------------------------------------
# LangChain tool wrappers
# ---------------------------------------------------------------------------


async def test_tool_get_tmdb_details(monkeypatch):
    fake = AsyncMock(return_value={"success": True, "data": {"number_of_seasons": 2}})
    monkeypatch.setattr(ma, "_execute_get_tmdb_details", fake)
    out = await ma.get_tmdb_details.ainvoke({"tmdb_id": "42", "media_type": "tv"})
    assert json.loads(out)["data"]["number_of_seasons"] == 2
    fake.assert_awaited_once_with("42", "tv")


async def test_tool_search_wikipedia(monkeypatch):
    fake = AsyncMock(return_value={"success": True, "data": [{"title": "T"}]})
    monkeypatch.setattr(ma, "_execute_search_wikipedia", fake)
    out = await ma.search_wikipedia.ainvoke({"query": "Q", "lang": "zh"})
    assert json.loads(out)["data"] == [{"title": "T"}]
    fake.assert_awaited_once_with("Q", "zh")


async def test_tool_get_wikipedia_page(monkeypatch):
    fake = AsyncMock(return_value={"success": True, "data": {"categories": ["C"]}})
    monkeypatch.setattr(ma, "_execute_get_wikipedia_page", fake)
    out = await ma.get_wikipedia_page.ainvoke({"title": "T", "lang": "ja"})
    assert json.loads(out)["data"]["categories"] == ["C"]
    fake.assert_awaited_once_with("T", "ja")


def test_tools_for_wikipedia_source():
    names = {t.name for t in _agent()._tools_for_source("wikipedia")}
    assert names == {"search_wikipedia", "get_wikipedia_page", "finalize"}


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def test_title_year_hint_content():
    hint = ma._title_year_hint(2024)
    assert "2024" in hint and "±1" in hint


def test_normalize_finalize_dates_copies_fallback_key():
    finalize = {"content_type": "movie",
                "matched_entity": {"first_air_date": "2021-05-01"}}
    ma._normalize_finalize_dates(finalize)
    assert finalize["matched_entity"]["release_date"] == "2021-05-01"


def test_normalize_finalize_dates_never_overwrites():
    finalize = {"content_type": "tv",
                "matched_entity": {"start_date": "2020-01-01",
                                   "first_air_date": "2019-01-01"}}
    ma._normalize_finalize_dates(finalize)
    assert finalize["matched_entity"]["start_date"] == "2020-01-01"


def test_normalize_finalize_dates_empty_entity_is_noop():
    finalize = {"content_type": "tv"}
    ma._normalize_finalize_dates(finalize)
    assert "matched_entity" not in finalize


def test_parse_genre_array_clamps_and_tolerates_garbage():
    assert ma._parse_genre_array('["Action", "Not A Real Genre"]') == ["Action"]
    assert ma._parse_genre_array("[this is not json]") == []
    assert ma._parse_genre_array("no array here") == []


# ---------------------------------------------------------------------------
# _attach_tmdb_episode_list
# ---------------------------------------------------------------------------


async def test_attach_tmdb_episode_list_skips_when_present(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(ma, "fetch_tmdb_episode_list", fetch)
    finalize = {"found": True, "content_type": "tv",
                "matched_entity": {"external_id": "tmdb:1",
                                   "seasons": [{"season_number": 1}],
                                   "episode_list": [{"episode": 1}]}}
    await ma._attach_tmdb_episode_list(finalize)
    fetch.assert_not_awaited()


async def test_attach_tmdb_episode_list_skips_without_tmdb_id(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(ma, "fetch_tmdb_episode_list", fetch)
    finalize = {"found": True, "content_type": "tv",
                "matched_entity": {"external_id": "wikipedia:zh:7",
                                   "external_source": "wikipedia",
                                   "seasons": [{"season_number": 1}]}}
    await ma._attach_tmdb_episode_list(finalize)
    fetch.assert_not_awaited()
    assert "episode_list" not in finalize["matched_entity"]


async def test_attach_tmdb_episode_list_fills_episodes(monkeypatch):
    episodes = [{"season": 1, "episode": 1}, {"season": 1, "episode": 2}]
    fetch = AsyncMock(return_value=episodes)
    monkeypatch.setattr(ma, "fetch_tmdb_episode_list", fetch)
    seasons = [{"season_number": 1, "episode_count": 2}]
    finalize = {"found": True, "content_type": "tv",
                "matched_entity": {"external_id": "tmdb:85937", "seasons": seasons}}
    await ma._attach_tmdb_episode_list(finalize)
    fetch.assert_awaited_once_with("85937", seasons)
    assert finalize["matched_entity"]["episode_list"] == episodes


async def test_attach_tmdb_episode_list_bare_digit_id(monkeypatch):
    fetch = AsyncMock(return_value=[{"season": 1, "episode": 1}])
    monkeypatch.setattr(ma, "fetch_tmdb_episode_list", fetch)
    finalize = {"found": True, "content_type": "tv",
                "matched_entity": {"external_id": "85937", "external_source": "tmdb",
                                   "seasons": [{"season_number": 1}]}}
    await ma._attach_tmdb_episode_list(finalize)
    fetch.assert_awaited_once_with("85937", [{"season_number": 1}])


async def test_attach_tmdb_episode_list_empty_result_not_attached(monkeypatch):
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(ma, "fetch_tmdb_episode_list", fetch)
    finalize = {"found": True, "content_type": "tv",
                "matched_entity": {"external_id": "tmdb:1",
                                   "seasons": [{"season_number": 1}]}}
    await ma._attach_tmdb_episode_list(finalize)
    assert "episode_list" not in finalize["matched_entity"]


# ---------------------------------------------------------------------------
# _ensure_genre
# ---------------------------------------------------------------------------


async def test_ensure_genre_skips_without_description():
    agent = _agent()
    agent._model = SimpleNamespace(
        ainvoke=AsyncMock(side_effect=AssertionError("LLM must not be called"))
    )
    finalize = {"matched_entity": {"title_cn": "X"}}
    await agent._ensure_genre(finalize)
    assert "genre" not in finalize["matched_entity"]


async def test_ensure_genre_skips_when_genre_present():
    agent = _agent()
    agent._model = SimpleNamespace(
        ainvoke=AsyncMock(side_effect=AssertionError("LLM must not be called"))
    )
    finalize = {"matched_entity": {"genre": ["Drama"], "description": "d"}}
    await agent._ensure_genre(finalize)
    assert finalize["matched_entity"]["genre"] == ["Drama"]


async def test_ensure_genre_infers_from_non_string_content():
    agent = _agent()
    # Some providers return structured content; it is JSON-encoded before parsing.
    agent._model = SimpleNamespace(
        ainvoke=AsyncMock(return_value=SimpleNamespace(content=["Action", "Bogus Genre"]))
    )
    finalize = {"matched_entity": {"title_cn": "X", "description": "热血冒险故事"}}
    await agent._ensure_genre(finalize)
    assert finalize["matched_entity"]["genre"] == ["Action"]


async def test_ensure_genre_llm_failure_leaves_genre_unset():
    agent = _agent()
    agent._model = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("boom")))
    finalize = {"matched_entity": {"title_cn": "X", "description": "d"}}
    await agent._ensure_genre(finalize)
    assert "genre" not in finalize["matched_entity"]


# ---------------------------------------------------------------------------
# AudioWork resolution passthrough
# ---------------------------------------------------------------------------


async def test_resolve_audio_work_delegates(monkeypatch):
    sentinel = ResourceMetadata(clean_title="A", found=True, content_type="asmr")
    fake = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(ma._resolver, "_resolve_audio_work", fake)
    agent = _agent()
    resource, channel, db = _ns_resource(), _ns_channel(), MagicMock()
    result = await agent._resolve_audio_work(resource, channel, db, "asmr", False)
    assert result is sentinel
    fake.assert_awaited_once_with(resource, channel, db, "asmr", False)


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


async def test_build_series_history_context_no_title_returns_none(monkeypatch):
    monkeypatch.setattr(metadata_service, "extract_search_title", lambda r: None)
    agent = _agent()
    # title_cn present but extraction yields nothing.
    assert await agent._build_series_history_context(
        SimpleNamespace(title_cn=None), MagicMock()
    ) is None


async def test_build_series_history_context_no_local_match_returns_none(monkeypatch):
    monkeypatch.setattr(metadata_service, "extract_search_title", lambda r: "某作品")
    monkeypatch.setattr(
        metadata_service, "match_series_by_title", AsyncMock(return_value=(None, 0))
    )
    agent = _agent()
    assert await agent._build_series_history_context(
        SimpleNamespace(title_cn="某作品"), MagicMock()
    ) is None


async def test_build_series_history_context_low_ratio_returns_none(monkeypatch):
    monkeypatch.setattr(
        metadata_service, "match_series_by_title",
        AsyncMock(return_value=(SimpleNamespace(id="s"), 50)),
    )
    agent = _agent()
    assert await agent._build_series_history_context(
        SimpleNamespace(search_title="某作品"), MagicMock()
    ) is None


async def test_build_series_history_context_with_siblings(
    db_session, sample_channel, monkeypatch
):
    from app.models.file_resource import FileResource
    from app.models.series import TVSeries

    series = TVSeries(
        id=str(uuid.uuid4()), title_cn="某作品", season_number=1, number_of_episodes=12,
    )
    db_session.add(series)
    for i, (ep, absolute) in enumerate([(3, None), (13, 13)]):
        db_session.add(FileResource(
            id=str(uuid.uuid4()), channel_id=sample_channel.id, guid=f"hist-{i}",
            title_raw=f"[G] 某作品 - {absolute or ep} [1080p]",
            series_id=series.id, is_batch=False,
            season=1, episode=ep, absolute_episode=absolute,
            episode_confidence="manual",
            torrent_url=f"magnet:?xt=urn:btih:hist{i}",
        ))
    await db_session.commit()
    monkeypatch.setattr(
        metadata_service, "match_series_by_title", AsyncMock(return_value=(series, 85))
    )

    ctx = await _agent()._build_series_history_context(
        SimpleNamespace(search_title="某作品"), db_session
    )

    assert "某作品" in ctx
    assert "Per-season episode counts" in ctx
    assert "Past parsed releases" in ctx
    assert "absolute 13" in ctx  # absolute-numbered sibling carries its marker


async def test_build_same_title_context_lists_collisions(monkeypatch):
    monkeypatch.setattr(
        metadata_service, "_find_same_title_works",
        AsyncMock(return_value=[SimpleNamespace(), SimpleNamespace()]),
    )
    monkeypatch.setattr(
        metadata_service, "format_same_title_works_context", lambda works: "COLLISIONS"
    )
    agent = _agent()
    ctx = await agent._build_same_title_context(
        SimpleNamespace(search_title="攻壳机动队"), MagicMock()
    )
    assert ctx == "COLLISIONS"


async def test_build_same_title_context_single_work_returns_none(monkeypatch):
    monkeypatch.setattr(
        metadata_service, "_find_same_title_works", AsyncMock(return_value=[SimpleNamespace()])
    )
    agent = _agent()
    assert await agent._build_same_title_context(
        SimpleNamespace(search_title="X"), MagicMock()
    ) is None


async def test_build_same_title_context_no_title_returns_none(monkeypatch):
    monkeypatch.setattr(metadata_service, "extract_search_title", lambda r: None)
    agent = _agent()
    assert await agent._build_same_title_context(
        SimpleNamespace(title_cn=None), MagicMock()
    ) is None


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------


def test_build_title_only_message_appends_year_hint(monkeypatch):
    monkeypatch.setattr(ma, "extract_title_year", lambda t: 2024)
    msg = _agent()._build_title_only_message("Show 2024 - 01", "tmdb")
    assert "Source mode: TMDB" in msg
    assert "2024" in msg  # year hint line appended
    assert "Show 2024 - 01" in msg


def test_build_production_message_includes_hints_and_contexts():
    resource = SimpleNamespace(
        title_raw="[G] Show - 03", title_cn="标题", title_en=None,
        subtitle_groups=None, subtitle_group="G", episode=3, season=1,
        resolution="1080p", source=None, video_codec=None, audio_codec=None,
        subtitle_type=None, container=None, title_year=2021,
    )
    channel = SimpleNamespace(name="My Channel")
    msg = _agent()._build_production_message(
        resource, channel, "wikipedia",
        series_context="SERIES CTX", same_title_context="SAME TITLE CTX",
    )
    assert "Pre-parsed fields" in msg
    assert "title_cn: 标题" in msg
    assert "2021" in msg                # title_year hint
    assert "SERIES CTX" in msg
    assert "SAME TITLE CTX" in msg
    assert "Channel: My Channel" in msg


# ---------------------------------------------------------------------------
# ReAct execution + extraction
# ---------------------------------------------------------------------------


async def test_run_react_agent_error_is_wrapped_as_transient():
    agent = _agent()
    failing = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("graph broke")))
    agent._agent_for_source = lambda source: failing
    finalize, info = await agent._run_react("msg", "tmdb")
    assert finalize["found"] is False
    assert "Agent error: graph broke" in finalize["reason"]
    assert info["error"] == "graph broke"
    assert info["data_sources_used"] == ["tmdb"]


def test_extract_finalize_result_prefers_last_valid_call():
    agent = _agent()
    payload = {"found": True, "clean_title": "X", "content_type": "tv"}
    messages = [
        AIMessage(content="", tool_calls=[
            {"name": "finalize", "args": {"result_json": "{not json"}, "id": "1"},
        ]),
        ToolMessage(content=json.dumps(payload), name="finalize", tool_call_id="1"),
    ]
    # The broken AIMessage args are skipped; the ToolMessage payload wins.
    assert agent._extract_finalize_result(messages) == payload


def test_extract_finalize_result_without_finalize_returns_default():
    agent = _agent()
    messages = [AIMessage(content="just text")]
    result = agent._extract_finalize_result(messages)
    assert result["found"] is False
    assert result["reason"] == "Agent did not call finalize"


def test_extract_finalize_result_toolmessage_invalid_json():
    agent = _agent()
    messages = [ToolMessage(content="{broken", name="finalize", tool_call_id="1")]
    assert agent._extract_finalize_result(messages)["found"] is False


def test_extract_search_info_collects_methods_and_errors():
    messages = [
        AIMessage(content="", tool_calls=[
            {"name": "get_tmdb_details", "args": {}, "id": "1"},
            {"name": "search_wikipedia", "args": {}, "id": "2"},
            {"name": "get_wikipedia_page", "args": {}, "id": "3"},
        ]),
        ToolMessage(content=json.dumps({"success": False, "error": "rate limited"}),
                    name="search_tmdb", tool_call_id="4"),
        ToolMessage(content=json.dumps({"success": True, "data": []}),
                    name="search_tmdb", tool_call_id="5"),
        ToolMessage(content="not json at all", name="search_tmdb", tool_call_id="6"),
        ToolMessage(content=json.dumps({"success": False, "error": "details exploded"}),
                    name="get_tmdb_details", tool_call_id="7"),
        ToolMessage(content="{broken", name="get_tmdb_details", tool_call_id="8"),
        ToolMessage(content="{also broken", name="search_wikipedia", tool_call_id="12"),
        ToolMessage(content=json.dumps({"success": False, "error": "Wikipedia request failed: timeout"}),
                    name="search_wikipedia", tool_call_id="9"),
        ToolMessage(content=json.dumps({"success": True, "data": []}),
                    name="search_wikipedia", tool_call_id="10"),
        ToolMessage(content=json.dumps({"success": False, "error": "Page not found"}),
                    name="get_wikipedia_page", tool_call_id="11"),
    ]
    info = UnifiedMetadataAgent._extract_search_info(messages)
    assert info["method"] == "tmdb|wikipedia"
    assert info["data_sources_used"] == ["tmdb", "wikipedia"]
    # First error wins via setdefault; later failures don't overwrite.
    assert info["source_errors"]["tmdb"] == "rate limited"
    assert info["source_errors"]["wikipedia"] == "Wikipedia request failed: timeout"
    assert info["error"] == "TMDB: rate limited"


# ---------------------------------------------------------------------------
# _maybe_web_fallback
# ---------------------------------------------------------------------------


def _fallback_probe_agent(monkeypatch, fb_return):
    fb = AsyncMock(return_value=fb_return)
    monkeypatch.setattr(ma, "web_fallback_judge", fb)
    return _agent(), fb


async def test_web_fallback_skipped_when_found(monkeypatch):
    agent, fb = _fallback_probe_agent(monkeypatch, None)
    finalize = {"found": True}
    result, info = await agent._maybe_web_fallback(finalize, {"error": None}, "T")
    assert result is finalize
    fb.assert_not_awaited()


async def test_web_fallback_skipped_on_transient_primary_failure(monkeypatch):
    agent, fb = _fallback_probe_agent(monkeypatch, None)
    finalize = {"found": False, "reason": "Agent error: Request timed out."}
    result, _info = await agent._maybe_web_fallback(finalize, {"error": None}, "T")
    assert result is finalize
    fb.assert_not_awaited()


async def test_web_fallback_disabled_returns_primary_verdict(monkeypatch):
    agent, fb = _fallback_probe_agent(monkeypatch, None)
    finalize = {"found": False, "reason": "No matching work found"}
    result, _info = await agent._maybe_web_fallback(finalize, {"error": None}, "T")
    assert result is finalize
    fb.assert_awaited_once()


async def test_web_fallback_error_marks_transient(monkeypatch):
    agent, _fb = _fallback_probe_agent(monkeypatch, (
        {"found": False},
        {"source_errors": {"wigolo": "HTTP 502"}, "data_sources_used": ["wigolo"],
         "error": "web search failed: HTTP 502"},
    ))
    finalize = {"found": False, "content_type": "tv", "reason": "No matching work found"}
    info = {"error": None, "source_errors": {"tmdb": "no results"},
            "data_sources_used": ["tmdb"], "method": None}
    result, out_info = await agent._maybe_web_fallback(finalize, info, "RAW TITLE")
    assert result["found"] is False
    assert result["reason"] == "web search failed: HTTP 502"
    assert result["clean_title"] == "RAW TITLE"
    assert out_info["error"] == "web search failed: HTTP 502"
    assert out_info["method"] == "react_then_web_fallback"
    assert out_info["source_errors"] == {"tmdb": "no results", "wigolo": "HTTP 502"}
    assert out_info["data_sources_used"] == ["tmdb", "wigolo"]


async def test_web_fallback_definitive_result_adopted(monkeypatch):
    agent, _fb = _fallback_probe_agent(monkeypatch, (
        {"found": True, "matched_entity": {"external_id": "tmdb:9"}},
        {"source_errors": {}, "data_sources_used": ["wigolo"], "error": None},
    ))
    finalize = {"found": False, "reason": "No matching work found"}
    result, out_info = await agent._maybe_web_fallback(finalize, {"error": None}, "RAW")
    assert result["found"] is True
    assert result["clean_title"] == "RAW"     # setdefault from the raw title
    assert result["content_type"] == "tv"
    assert out_info["method"] == "react_then_web_fallback"


# ---------------------------------------------------------------------------
# process() pipeline branches
# ---------------------------------------------------------------------------


async def test_process_empty_raw_title_returns_none():
    agent = _agent()
    result = await agent.process(SimpleNamespace(title_raw="   "), _ns_channel(), MagicMock())
    assert result is None


async def test_process_short_circuits_known_movie():
    agent = _agent()
    agent._get_cache = AsyncMock(return_value=None)
    agent._find_known_work = AsyncMock(return_value=("movie", "mid-1"))
    resource = _ns_resource(title_raw="[G] Some Film [1080p]")
    res = await agent.process(resource, _ns_channel(), MagicMock())
    assert res.found is True and res.content_type == "movie"
    assert resource.movie_id == "mid-1"
    assert resource.series_id is None
    assert resource.metadata_failure_type is None
    assert resource.metadata_attempts == 1


async def test_process_short_circuits_known_series_with_reconcile(monkeypatch):
    async def fake_reconcile(db, resource, series=None):
        # Absolute-numbered release located into the per-season numbering.
        resource.season, resource.episode = 2, 3

    monkeypatch.setattr(
        metadata_service, "reconcile_linked_series_resource", fake_reconcile
    )
    agent = _agent()
    agent._get_cache = AsyncMock(return_value=None)
    agent._find_known_work = AsyncMock(return_value=("tv", "sid-1"))
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(id="sid-1"))
    resource = _ns_resource(
        title_raw="[G] 某作品 - 15 [1080p]", season=None, episode=15,
        absolute_episode=15, search_title=None, title_cn="某作品", title_en=None,
    )
    res = await agent.process(resource, _ns_channel(), db)
    assert res.found is True
    assert resource.series_id == "sid-1"
    assert (resource.season, resource.episode) == (2, 3)
    # Missing search_title is repaired from the parsed Chinese title.
    assert resource.search_title == "某作品"


async def test_process_cached_not_found_applied_after_short_circuit_miss():
    agent = _agent()
    cached = ResourceMetadata(
        clean_title="X", found=False, content_type="tv",
        reason="No matching work found anywhere",
    )
    agent._get_cache = AsyncMock(return_value=cached)
    agent._find_known_work = AsyncMock(return_value=None)
    agent._apply_to_resource = AsyncMock()
    agent._run_react = AsyncMock()
    resource = _ns_resource()
    res = await agent.process(resource, _ns_channel(), MagicMock())
    assert res is cached
    agent._apply_to_resource.assert_awaited_once()
    agent._run_react.assert_not_called()  # cached miss stands, no live run
    assert resource.metadata_failure_type == "not_found"


async def test_process_non_media_marks_non_work_and_caches(monkeypatch):
    monkeypatch.setattr(ma, "_is_non_media", lambda t: True)
    agent = _agent()
    agent._get_cache = AsyncMock(return_value=None)
    agent._find_known_work = AsyncMock(return_value=None)
    agent._apply_to_resource = AsyncMock()
    agent._set_cache = AsyncMock()
    resource = _ns_resource(title_raw="BitComet Stable 2.21 解锁豪华版")
    res = await agent.process(resource, _ns_channel(), MagicMock())
    assert res.found is False
    assert "non-media" in res.reason
    agent._apply_to_resource.assert_awaited_once()
    agent._set_cache.assert_awaited_once()  # non_work is definitive → cached


async def test_process_audio_work_resolution(monkeypatch):
    monkeypatch.setattr(ma, "_detect_audio_work_type", lambda t: "asmr")
    agent = _agent()
    agent._get_cache = AsyncMock(return_value=None)
    agent._find_known_work = AsyncMock(return_value=None)
    agent._set_cache = AsyncMock()
    agent._resolve_audio_work = AsyncMock(return_value=ResourceMetadata(
        clean_title="A", found=True, content_type="asmr",
        matched_entity={"external_id": "x"},
    ))
    res = await agent.process(_ns_resource(), _ns_channel(), MagicMock())
    assert res.content_type == "asmr"
    agent._set_cache.assert_awaited_once()


async def test_process_audio_miss_falls_through_to_unavailable_source_run(monkeypatch):
    """Audio detection passes through (no audio verdict), the channel's source
    is unavailable (warning path), and the wikipedia judge pipeline runs."""
    monkeypatch.setattr(ma, "_detect_audio_work_type", lambda t: None)
    monkeypatch.setattr(ma, "_is_non_media", lambda t: False)
    monkeypatch.setattr(ma, "is_metadata_source_available", lambda source: False)
    agent = _agent()
    agent._get_cache = AsyncMock(return_value=None)
    agent._find_known_work = AsyncMock(return_value=None)
    agent._resolve_audio_work = AsyncMock(return_value=None)
    agent._run_search_then_judge = AsyncMock(return_value=(
        {"found": False, "clean_title": "Show", "content_type": "tv",
         "reason": "no match anywhere"},
        _found_info(),
    ))
    agent._apply_to_resource = AsyncMock()
    agent._set_cache = AsyncMock()
    # No title_cn/search_title attrs → both context builders return None fast.
    resource = SimpleNamespace(
        title_raw="[G] Show - 01 [1080p]", series_id=None, movie_id=None,
        metadata_attempts=0, last_metadata_attempt_at=None,
        metadata_failure_type=None,
    )
    res = await agent.process(resource, _ns_channel(), MagicMock())
    assert res.found is False
    assert res.search_method == "search_then_judge"
    agent._run_search_then_judge.assert_awaited_once()
    agent._set_cache.assert_awaited_once()  # definitive not_found cached


# ---------------------------------------------------------------------------
# process_title_only branches
# ---------------------------------------------------------------------------


async def test_process_title_only_empty_title():
    res = await _agent().process_title_only("   ")
    assert res.found is False
    assert res.reason == "Empty title"


async def test_process_title_only_without_api_key(monkeypatch):
    monkeypatch.setattr("app.services.runtime_config._overrides", {"llm_api_key": ""})
    res = await _agent().process_title_only("Some Title")
    assert res.found is False
    assert res.reason == "LLM API key not configured"


async def test_process_title_only_bangumi_carries_season_hint(monkeypatch):
    run_bangumi = AsyncMock(return_value=(
        {"found": True, "clean_title": "Show S2", "content_type": "tv",
         "matched_entity": {"external_id": "bangumi:1", "external_source": "bangumi"}},
        {"method": "bangumi_search_then_judge", "data_sources_used": ["bangumi"],
         "source_errors": {}, "error": None},
    ))
    monkeypatch.setattr(
        "app.services.metadata_bangumi.run_bangumi_search_then_judge", run_bangumi
    )
    res = await _agent().process_title_only("Show S2 - 01", "bangumi", season_hint=2)
    assert res.found is True
    assert res.search_method == "bangumi_search_then_judge"
    run_bangumi.assert_awaited_once()
    # The season hint travels as a lightweight resource stand-in.
    assert run_bangumi.await_args.kwargs["resource"].season == 2


async def test_process_title_only_tmdb_react_with_disabled_fallback(monkeypatch):
    agent = _agent()
    agent._run_react = AsyncMock(return_value=(
        {"found": False, "clean_title": "X", "content_type": "tv",
         "reason": "No matching work found"},
        {"method": None, "data_sources_used": ["tmdb"], "source_errors": {}, "error": None},
    ))
    fb = AsyncMock(return_value=None)  # fallback disabled
    monkeypatch.setattr(ma, "web_fallback_judge", fb)
    res = await agent.process_title_only("Obscure Title 2020", "tmdb")
    assert res.found is False
    assert res.data_sources_used == ["tmdb"]
    fb.assert_awaited_once()
    agent._run_react.assert_awaited_once()
