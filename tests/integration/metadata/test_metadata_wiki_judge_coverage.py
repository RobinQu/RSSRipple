"""Wikipedia search-then-judge coverage (``metadata_wiki_judge``).

All external surfaces are stubbed in-process: wikipedia search/page fetches
and wikitext retrieval are monkeypatched module attributes, the LLM judge is
a fake ``model.ainvoke``, and the ReAct fallback is an ``AsyncMock`` — no
network, no LLM.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import metadata_wiki_judge as mwj


class _FakeJudge:
    """Stands in for the LangChain chat model used by the judge step."""

    def __init__(self, content=None, exc=None):
        self._content = content
        self._exc = exc
        self.calls: list = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(content=self._content)


def _react(finalize=None, info=None):
    return AsyncMock(return_value=(
        finalize if finalize is not None else {"found": False, "clean_title": "", "content_type": "tv"},
        info if info is not None else {"method": "react", "data_sources_used": ["wikipedia"],
                                       "source_errors": {}, "error": None},
    ))


def _msg_builder(raw_title, source):
    return f"msg:{source}:{raw_title}"


def _patch_queries(monkeypatch, queries):
    monkeypatch.setattr(mwj, "_candidate_queries", lambda raw, resource: queries)


def _patch_wiki(monkeypatch, search=None, page=None):
    """search: async fn(query, lang) or mapping (q, lang)->result/exception.
    page: async fn(title, lang) or mapping title->result/exception."""
    async def fake_search(q, lang):
        value = search(q, lang) if callable(search) else search.get((q, lang))
        if isinstance(value, Exception):
            raise value
        return value

    async def fake_page(title, lang):
        value = page(title, lang) if callable(page) else page.get(title)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(mwj, "_execute_search_wikipedia", fake_search)
    monkeypatch.setattr(mwj, "_execute_get_wikipedia_page", fake_page)


# ---------------------------------------------------------------------------
# _parse_finalize_json
# ---------------------------------------------------------------------------


def test_parse_finalize_json_empty_and_braceless():
    assert mwj._parse_finalize_json("") is None
    assert mwj._parse_finalize_json(None) is None
    assert mwj._parse_finalize_json("no braces at all") is None


def test_parse_finalize_json_invalid_object():
    assert mwj._parse_finalize_json('{"a": }') is None
    # A JSON array is valid JSON but not the expected object.
    assert mwj._parse_finalize_json('[1, 2]') is None


def test_parse_finalize_json_fenced_and_embedded():
    obj = {"found": False, "reason": "x"}
    assert mwj._parse_finalize_json(f"```json\n{json.dumps(obj)}\n```") == obj
    assert mwj._parse_finalize_json(f"thinking... {json.dumps(obj)} done") == obj


# ---------------------------------------------------------------------------
# _attach_wikipedia_content
# ---------------------------------------------------------------------------


async def test_attach_wikipedia_content_no_title_is_noop(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(mwj, "fetch_wikipedia_wikitext", fetch)
    me = {"title_cn": "X"}
    await mwj._attach_wikipedia_content(me, {"lang": "zh"})
    assert me == {"title_cn": "X"}
    fetch.assert_not_called()


async def test_attach_wikipedia_content_merges_seasons_episodes(monkeypatch):
    monkeypatch.setattr(mwj, "fetch_wikipedia_wikitext", AsyncMock(return_value="WT"))
    monkeypatch.setattr(mwj, "has_tvanime_infobox", lambda wt: True)
    monkeypatch.setattr(mwj, "has_animanga_film_infobox", lambda wt: False)
    monkeypatch.setattr(
        mwj, "parse_seasons_from_infobox",
        lambda wt: [{"season_number": 1, "episode_count": 12}],
    )
    monkeypatch.setattr(mwj, "parse_episode_list", lambda wt: {
        "seasons": [{"season_number": 1, "episode_count": 12}],
        "episodes": [
            {"season": 1, "episode": 2, "air_date": "2020-01-12"},
            {"season": 1, "episode": 1, "air_date": "2020-01-05"},
        ],
    })
    monkeypatch.setattr(
        mwj, "parse_season_air_dates",
        lambda wt: {1: {"air_date": "2020-01-04", "end_date": "2020-03-28"}},
    )

    me = {}
    await mwj._attach_wikipedia_content(me, {"title": "作品", "lang": "zh"})

    assert me["is_anime"] is True
    assert me["number_of_seasons"] == 1
    assert me["number_of_episodes"] == 12
    # Infobox broadcast dates overlaid onto the season entry.
    assert me["seasons"][0]["air_date"] == "2020-01-04"
    assert me["seasons"][0]["end_date"] == "2020-03-28"
    assert len(me["episode_list"]) == 2
    # start_date derives from the earliest EPISODE air date.
    assert me["start_date"] == "2020-01-05"


async def test_attach_wikipedia_content_prefers_longer_episode_list_seasons(monkeypatch):
    monkeypatch.setattr(mwj, "fetch_wikipedia_wikitext", AsyncMock(return_value="WT"))
    monkeypatch.setattr(mwj, "has_tvanime_infobox", lambda wt: False)
    monkeypatch.setattr(mwj, "has_animanga_film_infobox", lambda wt: False)
    # Infobox knows 1 season; the episode list already shows 2 (ongoing work).
    monkeypatch.setattr(
        mwj, "parse_seasons_from_infobox",
        lambda wt: [{"season_number": 1, "episode_count": 12}],
    )
    monkeypatch.setattr(mwj, "parse_episode_list", lambda wt: {
        "seasons": [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 3},
        ],
        "episodes": [],
    })
    monkeypatch.setattr(mwj, "parse_season_air_dates", lambda wt: None)

    me = {"start_date": "2019-01-01"}  # pre-existing date must not be overwritten
    await mwj._attach_wikipedia_content(me, {"title": "作品", "lang": "zh"})

    assert me["number_of_seasons"] == 2
    assert me["number_of_episodes"] == 15
    assert me["start_date"] == "2019-01-01"
    assert "episode_list" not in me  # empty episodes are not attached


async def test_attach_wikipedia_content_retries_via_langlink(monkeypatch):
    async def fake_fetch(title, lang):
        return {"作品": "", "サクラ": "WT-JA"}[title]

    monkeypatch.setattr(mwj, "fetch_wikipedia_wikitext", fake_fetch)
    monkeypatch.setattr(mwj, "has_tvanime_infobox", lambda wt: False)
    monkeypatch.setattr(mwj, "has_animanga_film_infobox", lambda wt: wt == "WT-JA")
    monkeypatch.setattr(
        mwj, "parse_seasons_from_infobox",
        lambda wt: [{"season_number": 1, "episode_count": 1}] if wt else None,
    )
    monkeypatch.setattr(mwj, "parse_episode_list", lambda wt: None)
    monkeypatch.setattr(
        mwj, "parse_season_air_dates",
        lambda wt: {1: {"air_date": "2021-10-01"}} if wt else None,
    )

    me = {}
    page = {"title": "作品", "lang": "zh", "langlinks": {"ja": "サクラ"}}
    await mwj._attach_wikipedia_content(me, page)

    # The zh page parsed empty; the ja langlink retry supplied the data.
    assert me["is_anime"] is True
    assert me["number_of_episodes"] == 1
    # No episodes → start_date falls back to the season broadcast date.
    assert me["start_date"] == "2021-10-01"


async def test_attach_wikipedia_content_total_parse_failure_leaves_entity(monkeypatch):
    monkeypatch.setattr(mwj, "fetch_wikipedia_wikitext", AsyncMock(return_value="WT"))
    monkeypatch.setattr(mwj, "has_tvanime_infobox", lambda wt: False)
    monkeypatch.setattr(mwj, "has_animanga_film_infobox", lambda wt: False)
    monkeypatch.setattr(mwj, "parse_seasons_from_infobox", lambda wt: None)
    monkeypatch.setattr(mwj, "parse_episode_list", lambda wt: None)

    me = {"title_cn": "X"}
    await mwj._attach_wikipedia_content(me, {"title": "作品", "lang": "zh"})
    assert me == {"title_cn": "X"}


# ---------------------------------------------------------------------------
# run_search_then_judge: ReAct fallbacks
# ---------------------------------------------------------------------------


async def test_no_candidate_queries_falls_back_to_react(monkeypatch):
    _patch_queries(monkeypatch, [])
    react = _react(finalize={"found": True, "clean_title": "X", "content_type": "tv"})
    result, info = await mwj.run_search_then_judge(
        _FakeJudge(), "RAW", react_runner=react, msg_builder=_msg_builder,
    )
    assert result["found"] is True
    assert info["method"] == "react"
    react.assert_awaited_once_with("msg:wikipedia:RAW", "wikipedia")


async def test_judge_call_exception_falls_back_to_react(monkeypatch):
    _patch_queries(monkeypatch, [("Q", "zh")])
    _patch_wiki(monkeypatch, search={("Q", "zh"): {"success": True, "data": []}}, page={})
    react = _react()
    await mwj.run_search_then_judge(
        _FakeJudge(exc=RuntimeError("llm down")), "RAW",
        react_runner=react, msg_builder=_msg_builder,
    )
    react.assert_awaited_once()


async def test_judge_unparseable_json_falls_back_to_react(monkeypatch):
    _patch_queries(monkeypatch, [("Q", "zh")])
    _patch_wiki(monkeypatch, search={("Q", "zh"): {"success": True, "data": []}}, page={})
    react = _react()
    await mwj.run_search_then_judge(
        _FakeJudge(content="definitely not json"), "RAW",
        react_runner=react, msg_builder=_msg_builder,
    )
    react.assert_awaited_once()


async def test_search_errors_are_collected_and_no_evidence_accepts_not_found(monkeypatch):
    _patch_queries(monkeypatch, [("A", "zh"), ("B", "en"), ("C", "ja")])
    _patch_wiki(monkeypatch, search={
        ("A", "zh"): TimeoutError("timed out"),          # exception → source_errors
        ("B", "en"): {"success": False, "error": "bad"},  # explicit failure payload
        ("C", "ja"): None,                                 # non-dict → "no result"
    }, page={})
    judge = _FakeJudge(content=json.dumps({
        "found": False, "clean_title": "RAW", "content_type": "tv", "reason": "no match",
    }))
    fb = AsyncMock(return_value=None)  # web fallback disabled
    monkeypatch.setattr(mwj, "web_fallback_judge", fb)
    react = _react()

    result, info = await mwj.run_search_then_judge(
        judge, "RAW", react_runner=react, msg_builder=_msg_builder,
    )

    assert info["source_errors"]["wikipedia:zh"].startswith("TimeoutError")
    assert info["source_errors"]["wikipedia:en"] == "bad"
    assert info["source_errors"]["wikipedia:ja"] == "no result"
    assert result["found"] is False
    assert info["method"] == "search_then_judge"
    # No evidence at all → the not-found verdict is accepted without ReAct.
    react.assert_not_awaited()


# ---------------------------------------------------------------------------
# run_search_then_judge: candidate pipeline + judge
# ---------------------------------------------------------------------------


def _six_query_setup(monkeypatch):
    """Three queries whose candidates exercise dedup, the 6-candidate cap,
    an empty-title entry, a page-fetch exception, and poster/langlink carry."""
    _patch_queries(monkeypatch, [("q1", "zh"), ("q2", "zh"), ("q3", "en")])

    def cand(pid, title, lang):
        return {"page_id": pid, "title": title, "lang": lang, "summary": f"sum{pid}"}

    search = {
        ("q1", "zh"): {"success": True, "data": [
            cand(1, "Alpha", "zh"), cand(2, "", "zh"), cand(3, "Gamma", "zh"),
        ]},
        ("q2", "zh"): {"success": True, "data": [
            cand(1, "Alpha", "zh"),  # duplicate page_id → skipped
            cand(4, "標題四", "zh"), cand(5, "Epsilon", "zh"),
        ]},
        ("q3", "en"): {"success": True, "data": [
            cand(6, "Zeta", "en"), cand(7, "Eta", "en"),  # 7th candidate never read
        ]},
    }
    page = {
        "Alpha": RuntimeError("page fetch exploded"),
        "標題四": {"data": {
            "categories": ["2020 anime television series debuts"],
            "summary": "第四季简介 " * 50,
            "url": "https://zh.wikipedia.org/?curid=4",
            "poster_url": "http://img/p4.jpg",
            "langlinks": {"en": "Title Four EN"},
            "langlink_pageids": {"en": 99},
        }},
    }
    _patch_wiki(monkeypatch, search=search, page=page)


async def test_judge_found_enriches_matched_entity_from_evidence(monkeypatch):
    _six_query_setup(monkeypatch)
    judge = _FakeJudge(content=json.dumps({
        "found": True, "clean_title": "標題四", "content_type": "tv",
        "matched_entity": {"external_id": "wikipedia:4"},
    }))
    attach = AsyncMock()
    monkeypatch.setattr(mwj, "_attach_wikipedia_content", attach)
    react = _react()

    result, info = await mwj.run_search_then_judge(
        judge, "RAW TITLE", react_runner=react, msg_builder=_msg_builder,
    )

    react.assert_not_awaited()
    assert info["method"] == "search_then_judge"
    # The page-fetch exception surfaced as a source error.
    assert info["source_errors"]["page:zh"].startswith("RuntimeError")
    me = result["matched_entity"]
    # Bare pageid got language-qualified from the evidence page.
    assert me["external_id"] == "wikipedia:zh:4"
    assert me["categories"] == ["2020 anime television series debuts"]
    assert me["description"].startswith("第四季简介")
    assert me["wikipedia_url"] == "https://zh.wikipedia.org/?curid=4"
    assert me["alt_titles"] == ["Title Four EN"]
    assert me["alt_external_ids"] == [{"source": "wikipedia", "id": "wikipedia:en:99"}]
    # Cross-language bridge: title slots backfilled from the page/langlinks.
    assert me["title_cn"] == "標題四"
    assert me["title_en"] == "Title Four EN"
    # TV match → deterministic wikitext attach ran.
    attach.assert_awaited_once()


async def test_judge_found_without_evidence_match_keeps_entity(monkeypatch):
    _patch_queries(monkeypatch, [("q", "zh")])
    _patch_wiki(
        monkeypatch,
        search={("q", "zh"): {"success": True, "data": [
            {"page_id": 1, "title": "Unrelated Page", "lang": "zh"},
        ]}},
        page={"Unrelated Page": {"data": {"categories": [], "summary": ""}}},
    )
    judge = _FakeJudge(content=json.dumps({
        "found": True, "clean_title": "X", "content_type": "movie",
        "matched_entity": {"external_id": "wikipedia:999", "title_cn": "X"},
    }))
    attach = AsyncMock()
    monkeypatch.setattr(mwj, "_attach_wikipedia_content", attach)

    result, info = await mwj.run_search_then_judge(
        judge, "RAW", react_runner=_react(), msg_builder=_msg_builder,
    )
    # The judged pageid is not in the evidence → nothing is overlaid, and the
    # movie content type never triggers the TV-only wikitext attach.
    assert result["matched_entity"]["external_id"] == "wikipedia:999"
    attach.assert_not_awaited()
    assert info["method"] == "search_then_judge"


async def test_judge_structured_content_list_is_joined(monkeypatch):
    _patch_queries(monkeypatch, [("q", "zh")])
    _patch_wiki(monkeypatch, search={("q", "zh"): {"success": True, "data": []}}, page={})
    parts = [SimpleNamespace(text='{"found": false,'), SimpleNamespace(text=' "reason": "x"}')]
    judge = _FakeJudge(content=parts)
    monkeypatch.setattr(mwj, "web_fallback_judge", AsyncMock(return_value=None))
    react = _react()

    result, _info = await mwj.run_search_then_judge(
        judge, "RAW", react_runner=react, msg_builder=_msg_builder,
    )
    assert result["found"] is False
    react.assert_not_awaited()  # no evidence → verdict accepted


async def test_resource_hints_reach_the_judge_prompt(monkeypatch):
    _patch_queries(monkeypatch, [("q", "zh")])
    _patch_wiki(monkeypatch, search={("q", "zh"): {"success": True, "data": []}}, page={})
    judge = _FakeJudge(content=json.dumps({"found": False, "reason": "no"}))
    monkeypatch.setattr(mwj, "web_fallback_judge", AsyncMock(return_value=None))
    resource = SimpleNamespace(title_cn="标题", title_en="Title", episode=3, season=2)

    await mwj.run_search_then_judge(
        judge, "RAW", resource=resource,
        react_runner=_react(), msg_builder=_msg_builder,
    )

    user_msg = judge.calls[0][1].content
    assert "Pre-parsed hints:" in user_msg
    assert "title_cn='标题'" in user_msg
    assert "episode=3" in user_msg


async def test_autolink_skip_non_work_defers_to_judge_then_react(monkeypatch):
    # Title-similar top result that is NOT a creative work must skip the
    # auto-link short-circuit and fall through to the LLM judge.
    _patch_queries(monkeypatch, [("ViuTV", "zh")])
    _patch_wiki(
        monkeypatch,
        search={("ViuTV", "zh"): {"success": True, "data": [
            {"page_id": 7, "title": "ViuTV", "lang": "zh", "summary": "电视台"},
        ]}},
        page={"ViuTV": {"data": {"categories": ["Television channels"], "summary": "电视台"}}},
    )
    monkeypatch.setattr(mwj, "_classify_wikipedia_page", lambda cats, summary: "non_work")
    judge = _FakeJudge(content=json.dumps({
        "found": False, "clean_title": "ViuTV", "content_type": "tv", "reason": "station page",
    }))
    monkeypatch.setattr(mwj, "web_fallback_judge", AsyncMock(return_value=None))
    react = _react()

    _result, _info = await mwj.run_search_then_judge(
        judge, "ViuTV", react_runner=react, msg_builder=_msg_builder,
    )

    judge_called = len(judge.calls) == 1  # judge ran (auto-link skipped)
    assert judge_called
    # found=False WITH evidence → ReAct second opinion.
    react.assert_awaited_once()


# ---------------------------------------------------------------------------
# run_search_then_judge: web fallback
# ---------------------------------------------------------------------------


async def _found_false_setup(monkeypatch):
    _patch_queries(monkeypatch, [("q", "zh")])
    _patch_wiki(monkeypatch, search={("q", "zh"): {"success": True, "data": []}}, page={})
    judge = _FakeJudge(content=json.dumps({
        "found": False, "clean_title": "RAW", "content_type": "tv", "reason": "no wiki match",
    }))
    react = _react()
    return judge, react


async def test_web_fallback_transient_error_short_circuits(monkeypatch):
    judge, react = await _found_false_setup(monkeypatch)
    monkeypatch.setattr(mwj, "web_fallback_judge", AsyncMock(return_value=(
        {"found": False},
        {"source_errors": {"wigolo": "HTTP 502"}, "error": "web search failed: HTTP 502"},
    )))

    result, info = await mwj.run_search_then_judge(
        judge, "RAW", react_runner=react, msg_builder=_msg_builder,
    )

    assert result["found"] is False
    assert result["reason"] == "web search failed: HTTP 502"
    assert info["error"] == "web search failed: HTTP 502"
    assert info["method"] == "search_then_web_fallback"
    assert info["data_sources_used"] == ["wikipedia", "wigolo"]
    assert info["source_errors"]["wigolo"] == "HTTP 502"
    react.assert_not_awaited()  # transient → never the ReAct second opinion


async def test_web_fallback_definitive_result_is_adopted(monkeypatch):
    judge, react = await _found_false_setup(monkeypatch)
    monkeypatch.setattr(mwj, "web_fallback_judge", AsyncMock(return_value=(
        {"found": True, "matched_entity": {"external_id": "bangumi:123",
                                           "external_source": "bangumi"}},
        {"source_errors": {}, "error": None},
    )))

    result, info = await mwj.run_search_then_judge(
        judge, "RAW", react_runner=react, msg_builder=_msg_builder,
    )

    assert result["found"] is True
    assert result["clean_title"] == "RAW"   # setdefault from the raw title
    assert result["content_type"] == "tv"
    assert info["method"] == "search_then_web_fallback"
    react.assert_not_awaited()


async def test_web_fallback_disabled_falls_through_with_no_evidence(monkeypatch):
    judge, react = await _found_false_setup(monkeypatch)
    fb = AsyncMock(return_value=None)
    monkeypatch.setattr(mwj, "web_fallback_judge", fb)

    result, info = await mwj.run_search_then_judge(
        judge, "RAW", react_runner=react, msg_builder=_msg_builder,
        fallback_sources=[],
    )

    fb.assert_awaited_once()
    assert fb.await_args.kwargs["fallback_sources"] == []
    assert result["found"] is False
    assert info["method"] == "search_then_judge"
    react.assert_not_awaited()  # still no evidence → verdict accepted


async def test_autolink_work_short_circuit(monkeypatch):
    """A title-identical work page auto-links without the LLM judge; the TV
    branch also triggers the deterministic wikitext attach."""
    _patch_queries(monkeypatch, [("黃泉使者", "zh")])
    _patch_wiki(
        monkeypatch,
        search={("黃泉使者", "zh"): {"success": True, "data": [
            {"page_id": 42, "title": "黃泉使者", "lang": "zh",
             "url": "https://zh.wikipedia.org/?curid=42", "summary": "漫画改编"},
        ]}},
        page={"黃泉使者": {"data": {
            "categories": ["2024 anime television series debuts"],
            "summary": "《黃泉使者》是日本电视动画",
            "langlinks": {"en": "Yomi no Tsugai"},
            "langlink_pageids": {"en": 77},
        }}},
    )
    attach = AsyncMock()
    monkeypatch.setattr(mwj, "_attach_wikipedia_content", attach)
    judge = _FakeJudge()
    react = _react()

    result, info = await mwj.run_search_then_judge(
        judge, "黃泉使者 - 01", react_runner=react, msg_builder=_msg_builder,
    )

    assert info["method"] == "search_then_autolink"
    assert result["found"] is True
    assert result["content_type"] == "tv"
    assert result["matched_entity"]["external_id"] == "wikipedia:zh:42"
    assert result["matched_entity"]["title_en"] == "Yomi no Tsugai"
    assert result["matched_entity"]["alt_external_ids"] == [
        {"source": "wikipedia", "id": "wikipedia:en:77"},
    ]
    attach.assert_awaited_once()          # tv → wikitext attach
    assert len(judge.calls) == 0          # no LLM judge call
    react.assert_not_awaited()
