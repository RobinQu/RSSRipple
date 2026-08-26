"""Tests for metadata_wiki_judge: JSON extraction, cross-language titles,
and the search-then-judge orchestration (auto-link, judge, web fallback,
ReAct fallback). Wikipedia HTTP and the LLM are mocked.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import metadata_wiki_judge as wj

_JUDGE = "app.services.metadata_wiki_judge"


def _runner():
    react_runner = AsyncMock(return_value=({"found": False, "via": "react"}, {"method": "react"}))
    msg_builder = MagicMock(side_effect=lambda raw, source: (raw, source))
    return react_runner, msg_builder


def _model(payload) -> AsyncMock:
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(
        content=payload if isinstance(payload, str) else json.dumps(payload)
    )
    return model


def _wiki_patches(search_return=None, search_side_effect=None, page_return=None, page_side_effect=None):
    search = AsyncMock(return_value=search_return, side_effect=search_side_effect)
    page = AsyncMock(return_value=page_return, side_effect=page_side_effect)
    return patch(f"{_JUDGE}._execute_search_wikipedia", search), patch(
        f"{_JUDGE}._execute_get_wikipedia_page", page
    ), search, page


# ---------------------------------------------------------------------------
# _parse_finalize_json
# ---------------------------------------------------------------------------


def test_parse_finalize_json_variants():
    assert wj._parse_finalize_json("") is None
    assert wj._parse_finalize_json(None) is None
    assert wj._parse_finalize_json('```json\n{"found": true}\n```') == {"found": True}
    assert wj._parse_finalize_json('prefix {"found": false} suffix') == {"found": False}
    assert wj._parse_finalize_json("no braces at all") is None
    assert wj._parse_finalize_json("{invalid json}") is None
    assert wj._parse_finalize_json("[1, 2, 3]") is None  # not a dict


# ---------------------------------------------------------------------------
# _cross_language_titles
# ---------------------------------------------------------------------------


def test_cross_language_titles():
    zh = wj._cross_language_titles({
        "lang": "zh", "title": "黃泉使者",
        "langlinks": {"en": "Daemons of the Shadow Realm", "ja": "黄泉のツガイ"},
    })
    assert zh["title_cn"] == "黃泉使者"
    assert zh["title_en"] == "Daemons of the Shadow Realm"
    assert set(zh["alt_titles"]) == {"Daemons of the Shadow Realm", "黄泉のツガイ"}

    en = wj._cross_language_titles({"lang": "en", "title": "Show", "langlinks": {}})
    assert en == {"title_cn": None, "title_en": "Show", "alt_titles": []}


# ---------------------------------------------------------------------------
# run_search_then_judge
# ---------------------------------------------------------------------------


async def test_no_candidate_queries_falls_back_to_react():
    rr, mb = _runner()
    with patch(f"{_JUDGE}._candidate_queries", return_value=[]):
        finalize, info = await wj.run_search_then_judge(
            AsyncMock(), "!!!", react_runner=rr, msg_builder=mb
        )
    assert finalize == {"found": False, "via": "react"}
    assert info == {"method": "react"}
    rr.assert_awaited_once()


async def test_auto_link_exact_title_match_skips_judge():
    rr, mb = _runner()
    search_p, page_p, _, _ = _wiki_patches(
        search_return={
            "success": True,
            "data": [{"title": "無職転生", "page_id": 5, "url": "http://w/5", "summary": "s"}],
        },
        page_return={"data": {
            "categories": ["2018年日本電視動畫", "改編自輕小說的動畫"],
            "summary": "TV anime series",
            "poster_url": "http://img/p.jpg",
            "langlinks": {"en": "Mushoku Tensei"},
        }},
    )
    model = AsyncMock()
    with search_p, page_p:
        finalize, info = await wj.run_search_then_judge(
            model, "無職転生", react_runner=rr, msg_builder=mb
        )
    assert finalize["found"] is True
    assert finalize["content_type"] == "tv"
    assert finalize["confidence"] == 0.9
    me = finalize["matched_entity"]
    assert me["external_id"] == "wikipedia:zh:5"  # language-qualified pageid
    assert me["title_en"] == "Mushoku Tensei"
    assert me["title_cn"] == "無職転生"
    assert info["method"] == "search_then_autolink"
    model.ainvoke.assert_not_called()
    rr.assert_not_called()


async def test_disambiguation_page_skips_auto_link_and_uses_judge():
    rr, mb = _runner()
    search_p, page_p, _, _ = _wiki_patches(
        search_return={
            "success": True,
            "data": [{"title": "無職転生", "page_id": 5, "url": "http://w/5"}],
        },
        page_return={"data": {"disambiguation": True, "summary": "may refer to"}},
    )
    model = _model({"found": False, "content_type": "tv"})
    with search_p, page_p, patch(f"{_JUDGE}.web_fallback_judge", AsyncMock(return_value=None)):
        finalize, info = await wj.run_search_then_judge(
            model, "無職転生", react_runner=rr, msg_builder=mb
        )
    model.ainvoke.assert_awaited_once()  # judge ran instead of auto-link
    # found=False with evidence and no fallback -> ReAct second opinion
    assert finalize == {"found": False, "via": "react"}
    rr.assert_awaited_once()


async def test_judge_found_enriches_matched_entity_from_evidence():
    rr, mb = _runner()
    search_p, page_p, _, _ = _wiki_patches(
        search_return={
            "success": True,
            "data": [{"title": "Totally Different ZZZ", "page_id": 123, "url": "http://w/1"}],
        },
        page_return={"data": {
            "categories": ["2018年日本電視動畫"],
            "summary": "anime summary",
            "langlinks": {"ja": "日本語タイトル"},
        }},
    )
    model = _model({
        "found": True, "clean_title": "X", "content_type": "tv",
        "matched_entity": {"external_id": "wikipedia:123", "external_source": "wikipedia"},
    })
    with search_p, page_p:
        finalize, info = await wj.run_search_then_judge(
            model, "abcd show", react_runner=rr, msg_builder=mb
        )
    assert finalize["found"] is True
    me = finalize["matched_entity"]
    assert me["categories"] == ["2018年日本電視動畫"]
    assert me["description"] == "anime summary"
    # evidence page is lang=en: the pageid is language-qualified and the
    # page URL carried onto the entity.
    assert me["external_id"] == "wikipedia:en:123"
    assert me["wikipedia_url"] == "http://w/1"
    # evidence page is lang=en, so its own title fills title_en; langlink -> alt
    assert me["title_en"] == "Totally Different ZZZ"
    assert me["alt_titles"] == ["日本語タイトル"]
    assert info["method"] == "search_then_judge"
    assert info["error"] is None


async def test_judge_found_with_unknown_page_id_keeps_entity_unchanged():
    rr, mb = _runner()
    search_p, page_p, _, _ = _wiki_patches(
        search_return={
            "success": True,
            "data": [{"title": "Totally Different ZZZ", "page_id": 123}],
        },
        page_return={"data": {"categories": [], "summary": ""}},
    )
    model = _model("placeholder")
    # structured (list) content from the model is flattened before parsing
    model.ainvoke.return_value = SimpleNamespace(
        content=[SimpleNamespace(text='{"found": true, "content_type": "movie", '
                                 '"matched_entity": {"external_id": "wikipedia:999"}}')]
    )
    with search_p, page_p:
        finalize, info = await wj.run_search_then_judge(
            model, "abcd show", react_runner=rr, msg_builder=mb
        )
    assert finalize["found"] is True
    assert finalize["clean_title"] == ""  # setdefault applied
    assert finalize["matched_entity"] == {"external_id": "wikipedia:999"}
    assert info["method"] == "search_then_judge"


async def test_judge_llm_failure_falls_back_to_react():
    rr, mb = _runner()
    search_p, page_p, _, _ = _wiki_patches(
        search_return={"success": True, "data": [{"title": "Zzz Qqq", "page_id": 1}]},
        page_return={"data": {}},
    )
    model = AsyncMock()
    model.ainvoke.side_effect = RuntimeError("llm down")
    with search_p, page_p:
        finalize, info = await wj.run_search_then_judge(
            model, "abcd show", react_runner=rr, msg_builder=mb
        )
    assert finalize == {"found": False, "via": "react"}
    rr.assert_awaited_once()


async def test_judge_unparseable_json_falls_back_to_react():
    rr, mb = _runner()
    search_p, page_p, _, _ = _wiki_patches(
        search_return={"success": True, "data": [{"title": "Zzz Qqq", "page_id": 1}]},
        page_return={"data": {}},
    )
    model = _model("definitely not json")
    with search_p, page_p:
        finalize, _ = await wj.run_search_then_judge(
            model, "abcd show", react_runner=rr, msg_builder=mb
        )
    assert finalize == {"found": False, "via": "react"}


async def test_judge_not_found_with_web_fallback_transient_error():
    rr, mb = _runner()
    search_p, page_p, _, _ = _wiki_patches(
        search_return={"success": True, "data": [{"title": "Zzz Qqq", "page_id": 1}]},
        page_return={"data": {}},
    )
    fb = AsyncMock(return_value=(
        {"found": False, "reason": "web search failed: RuntimeError: net"},
        {"method": "search_then_web_fallback", "source_errors": {"wigolo": "net"}, "error": "net"},
    ))
    model = _model({"found": False, "content_type": "tv"})
    with search_p, page_p, patch(f"{_JUDGE}.web_fallback_judge", fb):
        finalize, info = await wj.run_search_then_judge(
            model, "abcd show", react_runner=rr, msg_builder=mb
        )
    assert finalize["found"] is False
    assert finalize["reason"] == "net"
    assert info["error"] == "net"
    assert info["data_sources_used"] == ["wikipedia", "wigolo"]
    rr.assert_not_called()  # transient: no ReAct second opinion


async def test_judge_not_found_with_web_fallback_definitive_answer():
    rr, mb = _runner()
    search_p, page_p, _, _ = _wiki_patches(
        search_return={"success": True, "data": [{"title": "Zzz Qqq", "page_id": 1}]},
        page_return={"data": {}},
    )
    fb = AsyncMock(return_value=(
        {"found": True, "matched_entity": {"external_id": "bangumi:1"}},
        {"method": "search_then_web_fallback", "source_errors": {}, "error": None},
    ))
    model = _model({"found": False, "content_type": "tv"})
    with search_p, page_p, patch(f"{_JUDGE}.web_fallback_judge", fb):
        finalize, info = await wj.run_search_then_judge(
            model, "abcd show", react_runner=rr, msg_builder=mb
        )
    assert finalize["found"] is True
    assert finalize["clean_title"] == "abcd show"  # setdefault
    assert finalize["content_type"] == "tv"
    assert info["method"] == "search_then_web_fallback"
    assert info["error"] is None
    rr.assert_not_called()


async def test_judge_not_found_without_evidence_is_accepted():
    rr, mb = _runner()
    # Wikipedia itself failed -> no evidence, source_errors recorded
    search_p, page_p, _, _ = _wiki_patches(
        search_side_effect=RuntimeError("http 503"),
    )
    model = _model({"found": False, "content_type": "tv"})
    with search_p, page_p, patch(f"{_JUDGE}.web_fallback_judge", AsyncMock(return_value=None)):
        finalize, info = await wj.run_search_then_judge(
            model, "abcd show", react_runner=rr, msg_builder=mb
        )
    assert finalize["found"] is False
    assert info["method"] == "search_then_judge"
    assert "RuntimeError" in info["source_errors"]["wikipedia:en"]
    rr.assert_not_called()  # clear not-found (no evidence) accepted as-is


async def test_search_unsuccessful_response_records_source_error():
    rr, mb = _runner()
    search_p, page_p, _, _ = _wiki_patches(
        search_return={"success": False, "error": "bad response"},
    )
    model = _model({"found": False, "content_type": "tv"})
    with search_p, page_p, patch(f"{_JUDGE}.web_fallback_judge", AsyncMock(return_value=None)):
        _, info = await wj.run_search_then_judge(
            model, "abcd show", react_runner=rr, msg_builder=mb
        )
    assert info["source_errors"]["wikipedia:en"] == "bad response"


async def test_page_fetch_exception_is_recorded_and_judge_still_runs():
    rr, mb = _runner()
    search_p, page_p, _, _ = _wiki_patches(
        search_return={"success": True, "data": [{"title": "Zzz Qqq", "page_id": 1}]},
        page_side_effect=RuntimeError("page fetch failed"),
    )
    model = _model({"found": True, "clean_title": "Zzz", "content_type": "tv"})
    with search_p, page_p:
        finalize, info = await wj.run_search_then_judge(
            model, "abcd show", react_runner=rr, msg_builder=mb
        )
    assert finalize["found"] is True
    assert "RuntimeError" in info["source_errors"]["page:en"]


async def test_top_candidates_capped_at_six():
    rr, mb = _runner()
    queries = [("q1", "en"), ("q2", "en"), ("q3", "en")]
    candidates = [
        {"title": f"Cand {q} {i}", "page_id": int(f"{qi}{i}")}
        for qi, q in enumerate(queries) for i in range(3)
    ]
    search = AsyncMock(side_effect=[
        {"success": True, "data": candidates[0:3]},
        {"success": True, "data": candidates[3:6]},
        {"success": True, "data": candidates[6:9]},
    ])
    page = AsyncMock(return_value={"data": {}})
    model = _model({"found": True, "clean_title": "x", "content_type": "tv"})
    with (
        patch(f"{_JUDGE}._candidate_queries", return_value=queries),
        patch(f"{_JUDGE}._execute_search_wikipedia", search),
        patch(f"{_JUDGE}._execute_get_wikipedia_page", page),
    ):
        finalize, _ = await wj.run_search_then_judge(
            model, "raw", react_runner=rr, msg_builder=mb
        )
    assert finalize["found"] is True
    assert page.await_count == 6  # 9 candidates capped at 6 before page fetches



# ---------------------------------------------------------------------------
# _attach_wikipedia_content: start_date derivation
# ---------------------------------------------------------------------------


def _attach_patches(list_data):
    return (
        patch(f"{_JUDGE}.fetch_wikipedia_wikitext", AsyncMock(return_value="wikitext")),
        patch(f"{_JUDGE}.parse_seasons_from_infobox", return_value=None),
        patch(f"{_JUDGE}.parse_episode_list", return_value=list_data),
    )


async def test_attach_wikipedia_content_derives_start_date_from_air_dates():
    me = {}
    page = {"title": "黃泉使者", "lang": "zh"}
    episodes = [
        {"season": 1, "episode": 1, "title": "a", "subtitle": None, "air_date": "2023-10-08"},
        {"season": 1, "episode": 2, "title": "b", "subtitle": None, "air_date": None},
        {"season": 1, "episode": 3, "title": "c", "subtitle": None, "air_date": "2023-10-01"},
    ]
    list_data = {"seasons": [{"season_number": 1, "episode_count": 3}], "episodes": episodes}
    p1, p2, p3 = _attach_patches(list_data)
    with p1, p2, p3:
        await wj._attach_wikipedia_content(me, page)
    assert me["episode_list"] == episodes
    assert me["start_date"] == "2023-10-01"


async def test_attach_wikipedia_content_empty_air_dates_leave_start_date_absent():
    me = {}
    page = {"title": "黃泉使者", "lang": "zh"}
    episodes = [
        {"season": 1, "episode": 1, "title": "a", "subtitle": None, "air_date": None},
        {"season": 1, "episode": 2, "title": "b", "subtitle": None, "air_date": None},
    ]
    list_data = {"seasons": [{"season_number": 1, "episode_count": 2}], "episodes": episodes}
    p1, p2, p3 = _attach_patches(list_data)
    with p1, p2, p3:
        await wj._attach_wikipedia_content(me, page)
    assert me["episode_list"] == episodes
    assert "start_date" not in me


async def test_attach_wikipedia_content_keeps_existing_start_date():
    me = {"start_date": "2020-01-01"}
    page = {"title": "黃泉使者", "lang": "zh"}
    episodes = [
        {"season": 1, "episode": 1, "title": "a", "subtitle": None, "air_date": "2023-10-08"},
    ]
    list_data = {"seasons": [{"season_number": 1, "episode_count": 1}], "episodes": episodes}
    p1, p2, p3 = _attach_patches(list_data)
    with p1, p2, p3:
        await wj._attach_wikipedia_content(me, page)
    assert me["start_date"] == "2020-01-01"
