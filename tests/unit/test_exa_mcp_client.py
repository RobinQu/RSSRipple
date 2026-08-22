"""Tests for exa_mcp_client: SSE/JSON reply extraction and web_search_exa
text payload parsing. No network."""

import json

import pytest

from app.services.exa_mcp_client import (
    ExaMcpError,
    _parse_search_text,
    _parse_sse_json,
)


def test_parse_sse_json_from_event_stream():
    body = (
        'event: message\n'
        'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'
        '\n'
    )
    assert _parse_sse_json(body, 1) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_parse_sse_json_from_plain_json():
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}})
    assert _parse_sse_json(body, 2)["id"] == 2


def test_parse_sse_json_skips_other_ids_and_bad_lines():
    body = (
        'data: not-json\n'
        'data: {"jsonrpc":"2.0","id":9,"result":{}}\n'
        'data: {"jsonrpc":"2.0","id":3,"result":{"hit":1}}\n'
    )
    assert _parse_sse_json(body, 3)["result"] == {"hit": 1}


def test_parse_sse_json_missing_reply_raises():
    with pytest.raises(ExaMcpError):
        _parse_sse_json('data: {"jsonrpc":"2.0","id":1,"result":{}}\n', 2)


_SAMPLE = (
    "Title: 猫と竜\n"
    "URL: https://ja.wikipedia.org/wiki/%E7%8C%AB%E3%81%A8%E7%AB%9C\n"
    "Published: 2026-08-02T23:02:03.000Z\n"
    "Author: N/A\n"
    "Highlights:\n"
    "ファンタジー小説\n"
    "...\n"
    "アニメ化決定\n"
    "\n"
    "Title: Some Doc\n"
    "URL: https://example.com/page\n"
    "Published: 2024-01-01T00:00:00.000Z\n"
    "Author: N/A\n"
    "Highlights:\n"
    "free text body\n"
)


def test_parse_search_text_multiple_blocks():
    hits = _parse_search_text(_SAMPLE)
    assert len(hits) == 2
    assert hits[0]["title"] == "猫と竜"
    assert hits[0]["url"] == "https://ja.wikipedia.org/wiki/%E7%8C%AB%E3%81%A8%E7%AB%9C"
    assert "ファンタジー小説" in hits[0]["text"]
    assert "Highlights:" not in hits[0]["text"]
    assert hits[1]["title"] == "Some Doc"
    assert hits[1]["text"] == "free text body"


def test_parse_search_text_ignores_stray_title_lines():
    # A "Title: " line not followed by "URL: " is body text, not a new result.
    text = "Title: A\nURL: https://a.com\nHighlights:\nTitle: not a result\nmore\n"
    hits = _parse_search_text(text)
    assert len(hits) == 1
    assert "Title: not a result" in hits[0]["text"]


def test_parse_search_text_empty():
    assert _parse_search_text("") == []
    assert _parse_search_text("no results here") == []
