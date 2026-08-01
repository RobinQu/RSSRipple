"""Mock OpenAI-compatible chat-completions endpoint.

Serves deterministic canned responses so the LLM-dependent code paths
(feed analysis, LLM candidate pick) can be exercised in integration tests
without a real LLM. Routing is prompt-keyword based:

  - feed-analysis prompts (ANALYSIS_SYSTEM_PROMPT)  → canned field mapping
  - LLM candidate-pick prompts (agent_service)      → {"pick": 1, ...}
  - anything else                                   → "{}"

Supports both non-streaming and ``stream=true`` (SSE) requests.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter()

# Mirrors the example in app.services.feed_analyzer.ANALYSIS_SYSTEM_PROMPT:
# valid regexes for the mikanani-style test feeds, full metadata coverage so
# _calc_confidence reports "high".
CANNED_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_raw": {"source": "title"},
        "title_cn": {"source": "title", "regex": r"\]\s*(.+?)\s*/", "group": 1},
        "title_en": {"source": "title", "regex": r"/\s*(.+?)\s*-", "group": 1},
        "subtitle_group": {"source": "title", "regex": r"^\[([^\]]+)\]", "group": 1},
        "episode": {
            "source": "title",
            "regex": r"-\s*(\d+)\b",
            "group": 1,
            "transform": "int",
        },
        "resolution": {
            "source": "title",
            "regex": r"\b(1080p|720p|480p|2160p|4K)\b",
            "group": 1,
            "transform": "lowercase",
        },
        "source": {"source": "title", "regex": r"\b(WebRip|WEB-DL|BDRip|WEB)\b", "group": 1},
        "torrent_url": {"source": "enclosures[0].url"},
    },
}


def _route_content(messages: list) -> str:
    """Pick a canned response based on prompt keywords."""
    text = "\n".join(str(m.get("content", "")) for m in messages)
    if "RSS feed analyzer" in text or "field mapping rules" in text:
        return json.dumps(CANNED_FIELD_MAPPING, ensure_ascii=False)
    if "只返回 JSON" in text or "pick the best" in text.lower():
        return json.dumps({"pick": 1, "reason": "mock pick: first candidate"})
    return "{}"


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "id": f"call_mock_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _finalize_result(messages: list) -> dict:
    """Build the finalize result_json, routed by the work title in the prompt.

    Known canned works finalize found=true (drives the upsert + link path);
    anything else finalizes found=false (drives the not_found path).
    """
    text = "\n".join(str(m.get("content", "")) for m in messages)
    canned = None
    if "Frieren" in text or "芙莉莲" in text:
        canned = {
            "clean_title": "葬送的芙莉莲",
            "entity": {
                "content_type": "tv",
                "external_id": "mock-exa-frieren",
                "external_source": "exa",
                "title_cn": "葬送的芙莉莲",
                "title_en": "Frieren: Beyond Journey's End",
                "description": "Mock LLM metadata result for integration tests.",
                "genre": ["Anime", "Fantasy"],
                "rating": 9.0,
            },
        }
    elif "黄泉使者" in text or "Daemons" in text:
        canned = {
            "clean_title": "黄泉使者",
            "entity": {
                "content_type": "tv",
                "external_id": "mock-exa-daemons",
                "external_source": "exa",
                "title_cn": "黄泉使者",
                "title_en": "Daemons of the Shadow Realm",
                "description": "Mock LLM metadata result for integration tests.",
                "genre": ["Anime", "Action"],
                "rating": 8.4,
            },
        }
    if canned:
        result = {
            "found": True,
            "clean_title": canned["clean_title"],
            "content_type": "tv",
            "confidence": 0.95,
            "matched_entity": canned["entity"],
        }
    else:
        result = {
            "found": False,
            "clean_title": "unknown",
            "content_type": "tv",
            "reason": "mock: no match",
        }
    return {"result_json": json.dumps(result, ensure_ascii=False)}


def _route_tool_calls(messages: list, tools: list) -> tuple[str | None, list | None, str]:
    """State machine for the LangGraph ReAct loop.

    Returns (content, tool_calls, finish_reason). Driven purely by message
    history: no tool results yet → call the search tool; search result back →
    finalize; finalize result back → end the loop with plain content.
    """
    tool_names = {t.get("function", {}).get("name", "") for t in tools}
    tool_msgs = [m for m in messages if m.get("role") == "tool"]

    # LangChain omits the function name on tool messages, so detect a past
    # finalize call on the *assistant* messages instead: once finalize has
    # been called and answered, end the loop with plain content.
    finalize_called = any(
        tc.get("function", {}).get("name") == "finalize"
        for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    )
    if finalize_called and tool_msgs:
        return "Mock analysis complete.", None, "stop"

    if tool_msgs:
        # A search tool result came back → finalize.
        return None, [_tool_call("finalize", _finalize_result(messages))], "tool_calls"

    # First step: call whichever search tool the source bound.
    for candidate in ("search_exa_agent", "search_wikipedia", "search_tmdb", "search_jina"):
        if candidate in tool_names:
            return None, [_tool_call(candidate, {"query": "mock search query"})], "tool_calls"
    # No recognizable search tool → finalize immediately.
    return None, [_tool_call("finalize", _finalize_result(messages))], "tool_calls"


def _completion_payload(content: str, model: str) -> dict:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _tool_completion_payload(content: str | None, tool_calls: list, model: str, finish: str) -> dict:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": tool_calls,
                },
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _chunk_payload(model: str, delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model") or "mock-model"
    messages = body.get("messages") or []
    tools = body.get("tools") or []

    # LangGraph ReAct (tool-calling) requests get the tool state machine.
    if tools and not body.get("stream"):
        content, tool_calls, finish = _route_tool_calls(messages, tools)
        if tool_calls:
            return JSONResponse(_tool_completion_payload(content, tool_calls, model, finish))
        return JSONResponse(_completion_payload(content or "", model))

    # Model-name variants let tests drive the feed_analyzer parse/robustness
    # paths: empty content, unparseable JSON, markdown-fenced JSON.
    if "empty" in model:
        content = ""
    elif "badjson" in model:
        content = "this is not json at all"
    elif "markdown" in model:
        # Pure fenced block — the app's _parse_llm_json strips the fence.
        inner = json.dumps(CANNED_FIELD_MAPPING, ensure_ascii=False, indent=2)
        content = f"```json\n{inner}\n```"
    elif "escapes" in model:
        # Invalid single-backslash escapes (\s, \d) — invalid strict JSON,
        # repaired by the app's _fix_invalid_escapes before parsing.
        content = json.dumps(CANNED_FIELD_MAPPING, ensure_ascii=False).replace("\\\\", "\\")
    else:
        content = _route_content(messages)

    if body.get("stream"):
        async def gen():
            step = 24
            for i in range(0, len(content), step):
                piece = _chunk_payload(model, {"content": content[i:i + step]})
                yield f"data: {json.dumps(piece, ensure_ascii=False)}\n\n"
            tail = _chunk_payload(model, {}, finish="stop")
            yield f"data: {json.dumps(tail)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse(_completion_payload(content, model))
