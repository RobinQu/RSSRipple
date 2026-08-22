"""Minimal MCP (Streamable HTTP) client for the free Exa search server.

Pure leaf module - no DB, no LangGraph. Speaks just enough JSON-RPC 2.0 over
HTTP to call ``web_search_exa`` on the public Exa MCP endpoint
(``https://mcp.exa.ai/mcp``), which is free and needs no API key. Each search
performs a fresh handshake (initialize -> notifications/initialized ->
tools/call); sessions are intentionally not reused across calls.

Raises on transport/protocol/tool errors so callers can classify failures as
transient.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.services.runtime_config import runtime_config

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "rssripple", "version": "1.0"}


class ExaMcpError(RuntimeError):
    """Raised when the Exa MCP call fails (transport, protocol, or tool)."""


def _parse_sse_json(text: str, request_id: int) -> dict[str, Any]:
    """Extract the JSON-RPC reply for *request_id* from an SSE or JSON body."""
    if text.lstrip().startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if payload.get("id") == request_id:
            return payload
    raise ExaMcpError(f"no JSON-RPC reply for id={request_id} in MCP response")


async def _rpc(
    client: httpx.AsyncClient,
    url: str,
    session_id: str | None,
    message: dict[str, Any],
    request_id: int | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """POST one JSON-RPC message; returns (reply, session_id)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    resp = await client.post(url, json=message, headers=headers)
    new_session = resp.headers.get("mcp-session-id", session_id)
    if resp.status_code >= 400:
        raise ExaMcpError(f"MCP HTTP {resp.status_code}: {resp.text[:200]}")
    if request_id is None:  # notification: 202 with no body
        return None, new_session
    reply = _parse_sse_json(resp.text, request_id)
    if "error" in reply:
        raise ExaMcpError(f"MCP JSON-RPC error: {reply['error']}")
    return reply, new_session


def _parse_search_text(text: str) -> list[dict[str, Any]]:
    """Parse the Exa ``web_search_exa`` text payload into normalised hits.

    Each result block looks like::

        Title: <title>
        URL: <url>
        Published: <iso date>
        Author: <author or N/A>
        Highlights:
        <free text...>
    """
    lines = text.splitlines()
    starts = [
        i for i, line in enumerate(lines)
        if line.startswith("Title: ") and i + 1 < len(lines) and lines[i + 1].startswith("URL: ")
    ]
    hits: list[dict[str, Any]] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        block = lines[start:end]
        body = block[2:]  # skip Title/URL lines
        if body and body[0].startswith("Published:"):
            body = body[1:]
        if body and body[0].startswith("Author:"):
            body = body[1:]
        if body and body[0].strip() == "Highlights:":
            body = body[1:]
        hits.append({
            "title": block[0][len("Title: "):].strip(),
            "url": block[1][len("URL: "):].strip(),
            "text": "\n".join(body).strip(),
        })
    return hits


async def web_search(query: str, num_results: int = 5) -> list[dict[str, Any]]:
    """Run one ``web_search_exa`` call; returns hits with title/url/text."""
    url = runtime_config.exa_mcp_url
    async with httpx.AsyncClient(timeout=60.0) as client:
        reply, session_id = await _rpc(client, url, None, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        }, 1)
        assert reply is not None
        _, session_id = await _rpc(client, url, session_id, {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }, None)
        reply, _ = await _rpc(client, url, session_id, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "web_search_exa",
                       "arguments": {"query": query, "numResults": num_results}},
        }, 2)
    assert reply is not None
    result = reply.get("result") or {}
    if result.get("isError"):
        detail = " ".join(
            c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)
        )
        raise ExaMcpError(f"web_search_exa tool error: {detail[:200]}")
    texts = [
        c.get("text", "") for c in result.get("content", [])
        if isinstance(c, dict) and c.get("type") == "text"
    ]
    hits = _parse_search_text("\n".join(texts))
    logger.info("[exa_mcp] search done query=%r hits=%d", query[:120], len(hits))
    return hits


__all__ = ["ExaMcpError", "web_search", "_parse_search_text", "_parse_sse_json"]
