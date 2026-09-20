"""Untrusted model identities must be grounded in successful tool evidence."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.services import metadata_wiki_judge as wiki
from app.services.metadata_agent import UnifiedMetadataAgent
from tests.unit.test_metadata_wiki_judge import _model, _runner


@pytest.mark.parametrize("identity", ["wikipedia:en:999", "wikipedia:zh:123", "wikipedia:bogus"])
async def test_judge_rejects_identity_absent_from_language_qualified_evidence(identity):
    runner, builder = _runner()
    search = AsyncMock(return_value={"success": True, "data": [{"title": "Different ZZZ", "page_id": 123}]})
    page = AsyncMock(return_value={"success": True, "data": {"categories": ["TV series"], "summary": "series"}})
    model = _model({"found": True, "content_type": "tv", "matched_entity": {"external_id": identity}})
    with (
        patch.object(wiki, "_candidate_queries", return_value=[("input", "en")]),
        patch.object(wiki, "_execute_search_wikipedia", search),
        patch.object(wiki, "_execute_get_wikipedia_page", page),
    ):
        result, _ = await wiki.run_search_then_judge(model, "input", react_runner=runner, msg_builder=builder)
    assert result["found"] is False
    assert result.get("matched_entity") is None


async def test_same_pageid_in_two_editions_retains_both_candidates_and_exact_identity():
    runner, builder = _runner()

    async def search(query, lang):
        return {"success": True, "data": [{"title": "Different " + lang, "page_id": 123}]}

    async def page(title, lang):
        return {"success": True, "data": {"categories": [lang + " category"], "summary": lang + " summary"}}

    page_mock = AsyncMock(side_effect=page)
    model = _model({"found": True, "content_type": "movie", "matched_entity": {"external_id": "wikipedia:ja:123"}})
    with (
        patch.object(wiki, "_candidate_queries", return_value=[("input", "en"), ("input", "ja")]),
        patch.object(wiki, "_execute_search_wikipedia", side_effect=search),
        patch.object(wiki, "_execute_get_wikipedia_page", page_mock),
    ):
        result, _ = await wiki.run_search_then_judge(model, "input", react_runner=runner, msg_builder=builder)
    assert page_mock.await_count == 2
    assert result["matched_entity"]["external_id"] == "wikipedia:ja:123"
    assert result["matched_entity"]["categories"] == ["ja category"]


@pytest.mark.parametrize("identity,kind", [("tmdb:999", "tv"), ("tmdb:111", "movie")])
async def test_tmdb_react_rejects_id_or_media_type_not_in_tool_evidence(identity, kind):
    agent = UnifiedMetadataAgent()
    final = {
        "found": True,
        "content_type": kind,
        "matched_entity": {"external_id": identity, "external_source": "tmdb"},
    }
    messages = [
        ToolMessage(
            content=json.dumps(
                {"success": True, "data": [{"tmdb_id": 111, "external_id": "tmdb:111", "media_type": "tv"}]}
            ),
            name="search_tmdb",
            tool_call_id="search",
        ),
        AIMessage(
            content="", tool_calls=[{"id": "final", "name": "finalize", "args": {"result_json": json.dumps(final)}}]
        ),
    ]
    agent._agent_for_source = MagicMock(return_value=MagicMock(ainvoke=AsyncMock(return_value={"messages": messages})))
    result, _ = await agent._run_react("input", "tmdb")
    assert result["found"] is False
    assert result.get("matched_entity") is None


@pytest.mark.parametrize(
    "name,payload,status,expected",
    [
        ("search_tmdb", {"success": True, "data": [{"tmdb_id": 111, "media_type": "tv"}]}, "success", True),
        ("search_tmdb", {"success": True, "data": [{"external_id": "tmdb:111", "media_type": "tv"}]}, "success", True),
        ("get_tmdb_details", {"success": True, "data": {"tmdb_id": 111, "media_type": "tv"}}, "success", True),
        ("search_tmdb", {"success": False, "data": [{"tmdb_id": 111, "media_type": "tv"}]}, "success", False),
        ("search_tmdb", {"success": True, "data": [{"tmdb_id": 111, "media_type": "tv"}]}, "error", False),
        ("search_tmdb", {"success": True, "data": [{"tmdb_id": 111}]}, "success", False),
        ("finalize", {"success": True, "data": [{"tmdb_id": 111, "media_type": "tv"}]}, "success", False),
        (
            "search_tmdb",
            {"success": True, "data": [None, "malformed", {"tmdb_id": True, "media_type": "tv"}]},
            "success",
            False,
        ),
        ("search_tmdb", "invalid json", "success", False),
    ],
)
def test_tmdb_grounding_accepts_only_successful_typed_source_evidence(name, payload, status, expected):
    from app.services.metadata_identity_evidence import ground_tmdb_identity

    final = {"found": True, "content_type": "tv", "matched_entity": {"external_id": "tmdb:111"}}
    message = ToolMessage(
        content=json.dumps(payload) if not isinstance(payload, str) else payload,
        name=name,
        tool_call_id="evidence",
        status=status,
    )
    result = ground_tmdb_identity(final, [message])
    assert result["found"] is expected
    if expected:
        assert result["matched_entity"]["external_id"] == "tmdb:111"
    else:
        assert result["matched_entity"] is None
    assert final["found"] is True  # validation does not mutate caller input
