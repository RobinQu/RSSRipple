"""Feed analyzer in-process integration coverage.

``analyze_feed`` / ``analyze_feed_stream`` normally run against a live LLM
(the docker stack points them at app-llm's mock /v1/chat/completions). Here
the LLM surface is monkeypatched so the parsing / validation / confidence /
retry code paths count toward the integration coverage gate without external
calls.
"""

from __future__ import annotations

import json

import pytest

from app.services import feed_analyzer as fa

GOOD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_cn": {"source": "title"},
        "title_en": {"source": "title"},
        "subtitle_group": {"source": "title", "regex": r"^\[([^\]]+)\]", "group": 1},
        "episode": {"source": "title", "transform": "int"},
        "resolution": {"source": "title", "transform": "lowercase"},
        "source": {"source": "title"},
        "torrent_url": {"source": "enclosures[0].url"},
    },
}

ENTRIES = [
    {"title": "[字幕组] 攻壳机动队 - 04 [1080p][HEVC]", "enclosures": [{"url": "http://x/t.torrent"}]}
] * 3


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch):
    monkeypatch.setattr("app.services.runtime_config._overrides", {
        "llm_api_key": "fake-key",
        "llm_model": "fake-model",
        "llm_base_url": "http://llm.invalid/v1",
        "llm_enable_thinking": "0",
    })


@pytest.fixture
def force_openai(monkeypatch):
    monkeypatch.setattr(fa, "_is_openrouter", lambda: False)


@pytest.fixture
def force_openrouter(monkeypatch):
    monkeypatch.setattr(fa, "_is_openrouter", lambda: True)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_fix_invalid_escapes():
    assert fa._fix_invalid_escapes(r'{"a": "\d+"}') == r'{"a": "\\d+"}'
    assert fa._fix_invalid_escapes(r'{"a": "\n"}') == r'{"a": "\n"}'
    assert fa._fix_invalid_escapes(r'{"a": "x"}') == r'{"a": "x"}'


def test_extract_json_object():
    assert fa._extract_json_object("no braces") is None
    assert fa._extract_json_object('prose {"a": {"b": 1}} tail') == '{"a": {"b": 1}}'
    assert fa._extract_json_object(r'{"a": "}"}') == r'{"a": "}"}'


def test_parse_llm_json_variants():
    # Clean JSON.
    assert fa._parse_llm_json(json.dumps(GOOD_MAPPING))["field_mappings"]["episode"]
    # Markdown code fences.
    assert fa._parse_llm_json("```json\n" + json.dumps(GOOD_MAPPING) + "\n```")["field_mappings"]
    # Invalid escapes (\s, \d).
    assert fa._parse_llm_json(r'{"field_mappings": {"episode": {"regex": "\d+", "source": "title"}}}')[
        "field_mappings"
    ]["episode"]["regex"] == r"\d+"
    # A valid ``\\`` escape pair followed by a non-escape must not be doubled.
    assert fa._fix_invalid_escapes(r'{"regex": "^\\[a\\]"  }') == r'{"regex": "^\\[a\\]"  }'
    # JSON embedded in reasoning prose.
    parsed = fa._parse_llm_json(f"Let me think... {json.dumps(GOOD_MAPPING)} ...done")
    assert parsed["field_mappings"]["episode"]
    # Unparseable → JSONDecodeError.
    with pytest.raises(json.JSONDecodeError):
        fa._parse_llm_json("not json at all")


def test_extract_content_fallbacks():
    class Msg:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    assert fa._extract_content(Msg(content=" answer ")) == " answer "
    assert fa._extract_content(Msg(content="  ", reasoning_content="rc")) == "rc"
    assert fa._extract_content(Msg(content="", reasoning="r")) == "r"
    details = [type("D", (), {"summary": "d1"}), type("D", (), {"text": "d2"})]
    assert fa._extract_content(Msg(content="", reasoning_details=details)) == "d1\nd2"
    assert fa._extract_content(Msg(content="", reasoning_details=[])) == ""


def test_validate_mapping_formats():
    new = fa._validate_mapping(GOOD_MAPPING)
    assert new["list_locator"] == {"source": "entries"}
    assert new["field_mappings"]["episode"]["source"] == "title"
    # Old flat format gets wrapped.
    flat = fa._validate_mapping({"title_cn": {"source": "title"}, "bogus": {"x": 1}})
    assert flat["list_locator"] == {"source": "entries"}
    assert set(flat["field_mappings"]) == {"title_cn"}
    # Empty mapping passes through with empty field_mappings.
    assert fa._validate_mapping({}) == {"list_locator": {"source": "entries"}, "field_mappings": {}}


def test_calc_confidence_levels():
    assert fa._calc_confidence(GOOD_MAPPING) == "high"
    mid = {"field_mappings": {"title_cn": {"source": "t"}, "title_en": {"source": "t"},
                              "episode": {"source": "t"}}}
    assert fa._calc_confidence(mid) == "medium"
    low = {"field_mappings": {"torrent_url": {"source": "e"}}}
    assert fa._calc_confidence(low) == "low"


def test_is_openrouter():
    assert isinstance(fa._is_openrouter(), bool)


# ---------------------------------------------------------------------------
# analyze_feed
# ---------------------------------------------------------------------------


async def test_analyze_feed_success_openai(force_openai, monkeypatch):
    async def fake_call(messages):
        return json.dumps(GOOD_MAPPING)

    monkeypatch.setattr(fa, "_call_openai", fake_call)
    result = await fa.analyze_feed(ENTRIES)
    assert result["confidence"] == "high"
    assert result["field_mapping"]["field_mappings"]["episode"]


async def test_analyze_feed_success_openrouter(force_openrouter, monkeypatch):
    async def fake_call(messages):
        return json.dumps(GOOD_MAPPING)

    monkeypatch.setattr(fa, "_call_openrouter", fake_call)
    result = await fa.analyze_feed(ENTRIES)
    assert result["confidence"] == "high"


async def test_analyze_feed_empty_entries():
    result = await fa.analyze_feed([])
    assert result == {"field_mapping": {}, "sample_results": [], "confidence": "low"}


async def test_analyze_feed_no_api_key(monkeypatch):
    monkeypatch.setattr("app.services.runtime_config._overrides", {"llm_api_key": ""})
    result = await fa.analyze_feed(ENTRIES)
    assert result["confidence"] == "low"
    assert result["field_mapping"] == {}


async def test_analyze_feed_empty_llm_response_retries(force_openai, monkeypatch):
    calls = {"n": 0}

    async def fake_call(messages):
        calls["n"] += 1
        return "   "

    monkeypatch.setattr(fa, "_call_openai", fake_call)
    result = await fa.analyze_feed(ENTRIES)
    assert calls["n"] == 3  # all attempts exhausted
    assert result["confidence"] == "low"


async def test_analyze_feed_daily_limit_short_circuits(force_openai, monkeypatch):
    calls = {"n": 0}

    async def fake_call(messages):
        calls["n"] += 1
        raise RuntimeError("API per-day limit exceeded")

    monkeypatch.setattr(fa, "_call_openai", fake_call)
    result = await fa.analyze_feed(ENTRIES)
    assert calls["n"] == 1
    assert result["confidence"] == "low"


async def test_analyze_feed_rate_limit_retries_then_fails(force_openai, monkeypatch):
    calls = {"n": 0}

    async def fake_call(messages):
        calls["n"] += 1
        raise RuntimeError("rate limit per-min")

    monkeypatch.setattr(fa, "_call_openai", fake_call)
    result = await fa.analyze_feed(ENTRIES)
    assert calls["n"] == 3
    assert result["confidence"] == "low"


async def test_analyze_feed_generic_failure_retries(force_openai, monkeypatch):
    calls = {"n": 0}

    async def fake_call(messages):
        calls["n"] += 1
        raise ConnectionError("boom")

    monkeypatch.setattr(fa, "_call_openai", fake_call)
    result = await fa.analyze_feed(ENTRIES)
    assert calls["n"] == 3
    assert result["confidence"] == "low"


# ---------------------------------------------------------------------------
# analyze_feed_stream
# ---------------------------------------------------------------------------


async def test_analyze_feed_stream_success(force_openai, monkeypatch):
    async def fake_stream(messages):
        yield {"type": "delta", "content": "partial"}
        yield {"type": "done", "field_mapping": GOOD_MAPPING, "confidence": "high"}

    monkeypatch.setattr(fa, "_stream_openai", fake_stream)
    events = [e async for e in fa.analyze_feed_stream(ENTRIES)]
    assert [e["type"] for e in events] == ["delta", "done"]
    assert events[-1]["confidence"] == "high"


async def test_analyze_feed_stream_no_entries():
    events = [e async for e in fa.analyze_feed_stream([])]
    assert events == [{"type": "error", "message": "No entries to analyze"}]


async def test_analyze_feed_stream_no_api_key(monkeypatch):
    monkeypatch.setattr("app.services.runtime_config._overrides", {"llm_api_key": ""})
    events = [e async for e in fa.analyze_feed_stream(ENTRIES)]
    assert events[0]["type"] == "error"


async def test_analyze_feed_stream_stream_raises(force_openai, monkeypatch):
    async def bad_stream(messages):
        raise RuntimeError("stream broke")
        yield  # pragma: no cover - keep this an async generator

    monkeypatch.setattr(fa, "_stream_openai", bad_stream)
    events = [e async for e in fa.analyze_feed_stream(ENTRIES)]
    assert events == [{"type": "error", "message": "stream broke"}]
