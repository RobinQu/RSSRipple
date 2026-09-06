"""Deterministic evidence-boundary regressions; no live search/model required."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.metadata_web_fallback import _bind_selected_entity, web_fallback_judge

HIT = {"url": "https://bangumi.tv/subject/1", "external_source": "bangumi", "external_id": "bangumi:1"}


@pytest.mark.parametrize("entity", [
    {"url": "https://bangumi.tv/subject/999"},
    {"url": HIT["url"], "external_id": "bangumi:999"},
    {"url": HIT["url"], "external_source": "tmdb"},
    {"url": HIT["url"], "wikipedia_url": "https://en.wikipedia.org/wiki/Other"},
    {"title_cn": "unsupported"},
])
def test_ungrounded_identity_cannot_be_applied(entity):
    assert _bind_selected_entity(entity, [HIT]) is None


def test_exact_identity_without_url_is_bound_and_episode_backdoors_removed():
    entity = {"external_source": "bangumi", "external_id": "bangumi:1",
              "episode_list": [{"episode": 50}], "alt_external_ids": [{"source": "tmdb", "id": "999"}],
              "single_season_entry": True, "number_of_episodes": 50, "title_cn": "Work"}
    bound = _bind_selected_entity(entity, [HIT])
    assert bound == {**HIT, "title_cn": "Work"}
    assert "episode_list" in entity  # input is not mutated


async def test_offsite_hits_do_not_consume_the_query_budget_or_reach_judge():
    search = AsyncMock(side_effect=[
        [{"url": "https://example.org/blog", "external_source": "bangumi", "external_id": "bangumi:999"}],
        [HIT],
    ])
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content=json.dumps({"found": True, "matched_entity": HIT}))
    with patch("app.services.metadata_web_fallback._fallback_queries", return_value=["first", "second"]):
        result, info = await web_fallback_judge(model, "Work", web_searcher=search)
    assert result["matched_entity"]["external_id"] == "bangumi:1"
    assert search.await_count == 2
    assert info["error"] is None
    assert "example.org" not in model.ainvoke.call_args.args[0][1].content


async def test_only_visible_top_five_candidates_can_be_selected():
    search = AsyncMock(return_value=[{"url": f"https://bangumi.tv/subject/{i}"} for i in range(1, 7)])
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content=json.dumps({
        "found": True, "matched_entity": {"url": "https://bangumi.tv/subject/6"}}))
    result, info = await web_fallback_judge(model, "Work", web_searcher=search)
    assert not result["found"]
    assert "ungrounded identity" in info["error"]


async def test_search_error_after_filtered_hits_remains_transient():
    search = AsyncMock(side_effect=[[{"url": "https://example.org/blog"}], RuntimeError("offline")])
    with patch("app.services.metadata_web_fallback._fallback_queries", return_value=["first", "second"]):
        result, info = await web_fallback_judge(AsyncMock(), "Work", web_searcher=search)
    assert not result["found"]
    assert "offline" in info["error"]
