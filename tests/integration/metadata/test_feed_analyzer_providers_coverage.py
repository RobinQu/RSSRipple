"""Provider-level feed analyzer coverage (OpenRouter/OpenAI SDK surfaces).

Complements ``test_feed_analyzer_coverage.py`` (LLM-callable layer) by
exercising the real ``_call_openrouter`` / ``_stream_openrouter`` /
``_call_openai`` / ``_stream_openai`` bodies with the SDK clients replaced by
in-memory fakes — no network, no LLM. Also fills the small pure-helper gaps
(unbalanced JSON extraction, dict-form ``reasoning_details``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

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


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch):
    monkeypatch.setattr("app.services.runtime_config._overrides", {
        "llm_api_key": "fake-key",
        "llm_model": "fake-model",
        "llm_base_url": "http://llm.invalid/v1",
        "llm_enable_thinking": "0",
    })


def _chunk(content=None, reasoning=None, reasoning_content=None, empty_choices=False):
    if empty_choices:
        return SimpleNamespace(choices=[])
    delta = SimpleNamespace(
        content=content, reasoning=reasoning, reasoning_content=reasoning_content,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


class _AsyncChunkStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


class _FakeChat:
    """Shared chat facade; each send/create call pops one scripted attempt."""

    def __init__(self, scripts):
        self._scripts = list(scripts)

    async def send_async(self, **kwargs):
        return self._next()

    async def create(self, **kwargs):
        return self._next()

    def _next(self):
        script = self._scripts.pop(0) if self._scripts else []
        if isinstance(script, Exception):
            raise script
        return _AsyncChunkStream(script)


def _patch_openrouter(monkeypatch, scripts):
    import openrouter

    chat = _FakeChat(scripts)

    class FakeOpenRouter:
        def __init__(self, **kwargs):
            self.chat = chat

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(openrouter, "OpenRouter", FakeOpenRouter)


def _patch_openai(monkeypatch, scripts):
    chat = _FakeChat(scripts)

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=chat)

    monkeypatch.setattr(fa, "AsyncOpenAI", FakeOpenAI)


# ---------------------------------------------------------------------------
# Pure-helper gaps
# ---------------------------------------------------------------------------


def test_extract_json_object_unbalanced_returns_none():
    # An opening brace that never closes must yield None (not a partial cut).
    assert fa._extract_json_object('{"a": 1') is None
    assert fa._extract_json_object("") is None


def test_parse_llm_json_extracted_object_still_invalid():
    # Balanced braces around non-JSON content: the last-resort extraction
    # succeeds but the re-parse fails, so the function must raise.
    with pytest.raises(json.JSONDecodeError):
        fa._parse_llm_json('prefix {"a": } suffix')


def test_extract_content_dict_reasoning_details():
    msg = SimpleNamespace(
        content="",
        reasoning_details=[{"summary": "s1"}, {"text": "t2"}, {"other": "skipped"}],
    )
    assert fa._extract_content(msg) == "s1\nt2"


# ---------------------------------------------------------------------------
# _call_openrouter / call_llm dispatch
# ---------------------------------------------------------------------------


async def test_call_openrouter_accumulates_content(monkeypatch):
    _patch_openrouter(monkeypatch, scripts=[
        [_chunk(content='{"a": '), _chunk(content='1}'), _chunk(empty_choices=True)],
    ])
    result = await fa._call_openrouter([{"role": "user", "content": "x"}])
    assert result == '{"a": 1}'


async def test_call_openrouter_falls_back_to_reasoning(monkeypatch):
    _patch_openrouter(monkeypatch, scripts=[
        [_chunk(reasoning='{"a": 2}')],
    ])
    result = await fa._call_openrouter([{"role": "user", "content": "x"}])
    assert result == '{"a": 2}'


async def test_call_openrouter_reasoning_content_alias(monkeypatch):
    _patch_openrouter(monkeypatch, scripts=[
        [_chunk(reasoning_content="rc-answer")],
    ])
    result = await fa._call_openrouter([{"role": "user", "content": "x"}])
    assert result == "rc-answer"


async def test_call_llm_dispatches_openrouter(monkeypatch):
    monkeypatch.setattr(fa, "_is_openrouter", lambda: True)

    async def fake_or(messages):
        return "from-openrouter"

    monkeypatch.setattr(fa, "_call_openrouter", fake_or)
    assert await fa.call_llm([]) == "from-openrouter"


async def test_call_llm_dispatches_openai(monkeypatch):
    monkeypatch.setattr(fa, "_is_openrouter", lambda: False)

    async def fake_oa(messages):
        return "from-openai"

    monkeypatch.setattr(fa, "_call_openai", fake_oa)
    assert await fa.call_llm([]) == "from-openai"


# ---------------------------------------------------------------------------
# _stream_openrouter
# ---------------------------------------------------------------------------


async def test_stream_openrouter_success(monkeypatch):
    payload = json.dumps(GOOD_MAPPING)
    _patch_openrouter(monkeypatch, scripts=[
        [_chunk(content=payload[:10]), _chunk(empty_choices=True), _chunk(content=payload[10:])],
    ])
    events = [e async for e in fa._stream_openrouter([])]
    assert [e["type"] for e in events] == ["delta", "delta", "done"]
    assert events[-1]["confidence"] == "high"
    assert events[-1]["field_mapping"]["field_mappings"]["episode"]


async def test_stream_openrouter_parses_reasoning_when_content_empty(monkeypatch):
    _patch_openrouter(monkeypatch, scripts=[
        [_chunk(reasoning=json.dumps(GOOD_MAPPING))],
    ])
    events = [e async for e in fa._stream_openrouter([])]
    assert events[-1]["type"] == "done"
    assert events[-1]["confidence"] == "high"


async def test_stream_openrouter_daily_limit_aborts_without_retry(monkeypatch):
    _patch_openrouter(monkeypatch, scripts=[RuntimeError("API per-day limit exceeded")])
    events = [e async for e in fa._stream_openrouter([])]
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "per-day" in events[0]["message"]


async def test_stream_openrouter_generic_failure_retries_then_errors(monkeypatch):
    _patch_openrouter(monkeypatch, scripts=[
        ConnectionError("boom"), ConnectionError("boom"), ConnectionError("boom"),
    ])
    events = [e async for e in fa._stream_openrouter([])]
    # Two resets (attempts 2 and 3) then a terminal error.
    assert [e["type"] for e in events] == ["reset", "reset", "error"]
    assert events[-1]["message"] == "boom"


async def test_stream_openrouter_rate_limit_recovers(monkeypatch):
    payload = json.dumps(GOOD_MAPPING)
    _patch_openrouter(monkeypatch, scripts=[
        RuntimeError("rate limit per-min exceeded"),
        [_chunk(content=payload)],
    ])
    events = [e async for e in fa._stream_openrouter([])]
    assert events[0] == {"type": "reset"}
    assert events[-1]["type"] == "done"


async def test_stream_openrouter_empty_response_retries_then_errors(monkeypatch):
    _patch_openrouter(monkeypatch, scripts=[[], [], []])
    events = [e async for e in fa._stream_openrouter([])]
    assert events[-1] == {"type": "error", "message": "LLM returned empty response"}
    assert [e["type"] for e in events].count("reset") == 2


async def test_stream_openrouter_invalid_json_retries_then_errors(monkeypatch):
    _patch_openrouter(monkeypatch, scripts=[
        [_chunk(content="not json")],
        [_chunk(content="still not json")],
        [_chunk(content="nope")],
    ])
    events = [e async for e in fa._stream_openrouter([])]
    assert events[-1] == {"type": "error", "message": "LLM returned invalid JSON"}


# ---------------------------------------------------------------------------
# _stream_openai
# ---------------------------------------------------------------------------


async def test_stream_openai_skips_empty_choices_and_uses_reasoning(monkeypatch):
    _patch_openai(monkeypatch, scripts=[
        [
            _chunk(empty_choices=True),
            _chunk(reasoning=json.dumps(GOOD_MAPPING)),
        ],
    ])
    events = [e async for e in fa._stream_openai([])]
    assert [e["type"] for e in events] == ["delta", "done"]
    assert events[-1]["confidence"] == "high"


async def test_stream_openai_reasoning_content_alias_delta(monkeypatch):
    _patch_openai(monkeypatch, scripts=[
        [_chunk(reasoning_content=json.dumps(GOOD_MAPPING))],
    ])
    events = [e async for e in fa._stream_openai([])]
    assert events[-1]["type"] == "done"


async def test_stream_openai_empty_response_retries_then_errors(monkeypatch):
    _patch_openai(monkeypatch, scripts=[[], [], []])
    events = [e async for e in fa._stream_openai([])]
    assert events[-1] == {"type": "error", "message": "LLM returned empty response"}
    assert [e["type"] for e in events].count("reset") == 2


async def test_stream_openai_invalid_json_retries_then_errors(monkeypatch):
    _patch_openai(monkeypatch, scripts=[
        [_chunk(content="garbage")],
        [_chunk(content="garbage")],
        [_chunk(content="garbage")],
    ])
    events = [e async for e in fa._stream_openai([])]
    assert events[-1] == {"type": "error", "message": "LLM returned invalid JSON"}


async def test_stream_openai_exception_exhausts_attempts(monkeypatch):
    _patch_openai(monkeypatch, scripts=[
        ConnectionError("down"), ConnectionError("down"), ConnectionError("down"),
    ])
    events = [e async for e in fa._stream_openai([])]
    assert events[-1] == {"type": "error", "message": "down"}


async def test_stream_openai_recovers_after_transient_failure(monkeypatch):
    payload = json.dumps(GOOD_MAPPING)
    _patch_openai(monkeypatch, scripts=[
        ConnectionError("flaky"),
        [_chunk(content=payload)],
    ])
    events = [e async for e in fa._stream_openai([])]
    assert events[0] == {"type": "reset"}
    assert events[-1]["type"] == "done"
