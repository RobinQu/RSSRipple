"""The Wikipedia fallback graph must enforce the same identity boundary as judge."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.services.metadata_agent import UnifiedMetadataAgent


@pytest.mark.parametrize(
    "identity,expected",
    [
        ("wikipedia:en:123", True),
        ("wikipedia:en:999", False),
        ("wikipedia:zh:123", False),
    ],
)
async def test_wikipedia_react_grounding(identity, expected):
    agent = UnifiedMetadataAgent()
    final = {
        "found": True,
        "content_type": "movie",
        "matched_entity": {"external_id": identity, "external_source": "wikipedia"},
    }
    messages = [
        ToolMessage(
            name="search_wikipedia",
            tool_call_id="search",
            content=json.dumps(
                {
                    "success": True,
                    "data": [
                        {
                            "page_id": 123,
                            "title": "Synthetic film",
                            "url": "https://en.wikipedia.org/wiki/Synthetic_film",
                        }
                    ],
                }
            ),
        ),
        AIMessage(
            content="", tool_calls=[{"name": "finalize", "id": "final", "args": {"result_json": json.dumps(final)}}]
        ),
    ]
    agent._agent_for_source = MagicMock(return_value=MagicMock(ainvoke=AsyncMock(return_value={"messages": messages})))
    result, _ = await agent._run_react("input", "wikipedia")
    assert result["found"] is expected
    if not expected:
        assert result.get("matched_entity") is None


def tool(data, *, name="search_wikipedia", success=True, status="success"):
    return ToolMessage(
        name=name, tool_call_id="evidence", status=status, content=json.dumps({"success": success, "data": data})
    )


@pytest.mark.parametrize(
    "name,success,status,url",
    [
        ("search_wikipedia", False, "success", "https://en.wikipedia.org/wiki/Test"),
        ("search_wikipedia", True, "error", "https://en.wikipedia.org/wiki/Test"),
        ("finalize", True, "success", "https://en.wikipedia.org/wiki/Test"),
        ("search_wikipedia", True, "success", "https://en.wikipedia.org.attacker.invalid/wiki/Test"),
        ("search_wikipedia", True, "success", None),
    ],
)
def test_failed_or_unqualified_tool_evidence_is_rejected(name, success, status, url):
    from app.services.metadata_identity_evidence import ground_wikipedia_react_identity

    final = {"found": True, "matched_entity": {"external_id": "wikipedia:en:123"}}
    result = ground_wikipedia_react_identity(
        final, [tool([{"page_id": 123, "url": url}], name=name, success=success, status=status)]
    )
    assert not result["found"] and result["matched_entity"] is None


def test_duplicate_observations_preserve_details_and_trusted_language_aliases():
    from app.services.metadata_identity_evidence import ground_wikipedia_react_identity

    final = {
        "found": True,
        "matched_entity": {
            "external_id": "wikipedia:123",
            "alt_external_ids": [{"source": "wikipedia", "id": "wikipedia:ja:999"}],
            "wikipedia_url": "https://evil.invalid",
            "external_source": "tmdb",
        },
    }
    page = {"page_id": 123, "url": "https://en.wikipedia.org/wiki/Test"}
    detail = {**page, "categories": ["Animated films"], "langlink_pageids": {"ja": 456}}
    result = ground_wikipedia_react_identity(
        final, [tool([page]), tool(detail, name="get_wikipedia_page"), tool([page])]
    )
    entity = result["matched_entity"]
    assert result["found"]
    assert entity["external_id"] == "wikipedia:en:123"
    assert entity["external_source"] == "wikipedia"
    assert entity["wikipedia_url"] == page["url"]
    assert entity["categories"] == ["Animated films"]
    assert entity["alt_external_ids"] == [{"source": "wikipedia", "id": "wikipedia:ja:456"}]
    assert final["matched_entity"]["external_source"] == "tmdb"


def test_bare_page_id_rejected_when_two_editions_share_number():
    from app.services.metadata_identity_evidence import ground_wikipedia_react_identity

    result = ground_wikipedia_react_identity(
        {"found": True, "matched_entity": {"external_id": "wikipedia:123"}},
        [tool([{"page_id": 123, "url": f"https://{lang}.wikipedia.org/wiki/Test"} for lang in ("en", "ja")])],
    )
    assert not result["found"] and result["matched_entity"] is None


@pytest.mark.parametrize("entity", [None, [], "invalid", {"external_id": 123}])
def test_malformed_entity_fails_closed(entity):
    from app.services.metadata_identity_evidence import ground_wikipedia_identity

    result, page = ground_wikipedia_identity(
        {"found": True, "matched_entity": entity}, [{"page_id": 123, "lang": "en"}]
    )
    assert not result["found"] and result["matched_entity"] is None and page is None
