"""Unit tests for feed_analyzer service.

Mocks the OpenAI and OpenRouter clients to test analyze_feed() and
analyze_feed_stream() without real LLM calls. Covers both thinking
(reasoning) and non-thinking model response shapes.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.feed_analyzer import (
    _call_openai,
    _call_openrouter,
    _extract_content,
    _extract_json_object,
    _parse_llm_json,
    _stream_openai,
    _stream_openrouter,
    analyze_feed,
    analyze_feed_stream,
    call_llm,
)

# --- Test Data ---

SAMPLE_ENTRIES = [
    {
        "title": "[LoliHouse] Spy x Family - 12 [WebRip 1080p HEVC-10bit AAC].mkv",
        "enclosures": [{"url": "https://example.com/test.torrent", "length": "1234567"}],
        "link": "https://example.com/detail/123",
    },
    {
        "title": "[LoliHouse] Spy x Family - 13 [WebRip 1080p HEVC-10bit AAC].mkv",
        "enclosures": [{"url": "https://example.com/test2.torrent", "length": "2345678"}],
        "link": "https://example.com/detail/124",
    },
]

# 5 of 6 expected fields covered -> confidence "high" (83%)
# Expected fields: title_cn, title_en, subtitle_group, episode, resolution, source
MOCK_LLM_RESPONSE = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*-", "group": 1},
        "subtitle_group": {"source": "title", "regex": "^\\[([^\\]]+)\\]", "group": 1},
        "episode": {"source": "title", "regex": "-\\s*(\\d+)", "group": 1, "transform": "int"},
        "resolution": {"source": "title", "regex": "\\b(1080p|720p)\\b", "group": 1, "transform": "lowercase"},
        "source": {"source": "title", "regex": "\\b(WebRip|WEB-DL|BDRip)\\b", "group": 1},
        "torrent_url": {"source": "enclosures[0].url"},
    },
}

# Old flat format (no field_mappings wrapper)
MOCK_OLD_FORMAT_RESPONSE = {
    "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*-", "group": 1},
    "subtitle_group": {"source": "title", "regex": "^\\[([^\\]]+)\\]", "group": 1},
    "episode": {"source": "title", "regex": "-\\s*(\\d+)", "group": 1, "transform": "int"},
    "resolution": {"source": "title", "regex": "\\b(1080p|720p)\\b", "group": 1, "transform": "lowercase"},
    "source": {"source": "title", "regex": "\\b(WebRip|WEB-DL|BDRip)\\b", "group": 1},
    "torrent_url": {"source": "enclosures[0].url"},
}

# Partial coverage: 2 fields -> confidence "low"
MOCK_PARTIAL_RESPONSE = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*-", "group": 1},
        "subtitle_group": {"source": "title", "regex": "^\\[([^\\]]+)\\]", "group": 1},
    },
}

# Medium coverage: 3-4 expected fields -> confidence "medium"
MOCK_MEDIUM_RESPONSE = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*-", "group": 1},
        "subtitle_group": {"source": "title", "regex": "^\\[([^\\]]+)\\]", "group": 1},
        "episode": {"source": "title", "regex": "-\\s*(\\d+)", "group": 1, "transform": "int"},
        "resolution": {"source": "title", "regex": "\\b(1080p|720p)\\b", "group": 1, "transform": "lowercase"},
    },
}


def _setup_openai_mock(mock_openai_class, response_str: str):
    """Configure mock_openai_class to return a given JSON string as LLM response."""
    mock_choice = MagicMock()
    mock_choice.message.content = response_str
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    mock_openai_class.return_value = mock_client


# =============================================================================
# 1. test_analyze_feed_success
# =============================================================================
@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_success(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"

    _setup_openai_mock(mock_openai_class, json.dumps(MOCK_LLM_RESPONSE))

    result = await analyze_feed(SAMPLE_ENTRIES)

    assert result["confidence"] == "high"
    assert "field_mappings" in result["field_mapping"]
    assert "list_locator" in result["field_mapping"]
    assert "torrent_url" in result["field_mapping"]["field_mappings"]
    assert "episode" in result["field_mapping"]["field_mappings"]
    assert "title_cn" in result["field_mapping"]["field_mappings"]
    assert "subtitle_group" in result["field_mapping"]["field_mappings"]
    assert "resolution" in result["field_mapping"]["field_mappings"]


# =============================================================================
# 2. test_analyze_feed_no_api_key
# =============================================================================
@pytest.mark.asyncio
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_no_api_key(mock_settings):
    mock_settings.llm_api_key = ""

    result = await analyze_feed(SAMPLE_ENTRIES)

    assert result["confidence"] == "low"
    assert result["field_mapping"] == {}
    assert result["sample_results"] == []


# =============================================================================
# 3. test_analyze_feed_old_flat_format
# =============================================================================
@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_old_flat_format(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"

    _setup_openai_mock(mock_openai_class, json.dumps(MOCK_OLD_FORMAT_RESPONSE))

    result = await analyze_feed(SAMPLE_ENTRIES)

    # Old flat format should be wrapped with list_locator + field_mappings
    assert "field_mappings" in result["field_mapping"]
    assert "list_locator" in result["field_mapping"]
    assert result["field_mapping"]["list_locator"] == {"source": "entries"}
    assert "title_cn" in result["field_mapping"]["field_mappings"]
    assert "torrent_url" in result["field_mapping"]["field_mappings"]
    # 5 of 6 expected fields covered -> high confidence
    assert result["confidence"] == "high"


# =============================================================================
# 4. test_analyze_feed_invalid_json
# =============================================================================
@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_invalid_json(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"

    _setup_openai_mock(mock_openai_class, "this is not valid json {{{")

    result = await analyze_feed(SAMPLE_ENTRIES)

    assert result["confidence"] == "low"
    assert result["field_mapping"] == {}
    assert result["sample_results"] == []


# =============================================================================
# 5. test_analyze_feed_partial_coverage
# =============================================================================
@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_partial_coverage(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"

    _setup_openai_mock(mock_openai_class, json.dumps(MOCK_PARTIAL_RESPONSE))

    result = await analyze_feed(SAMPLE_ENTRIES)

    # Only 2 of 6 expected fields -> low confidence
    assert result["confidence"] == "low"
    assert "field_mappings" in result["field_mapping"]
    assert len(result["field_mapping"]["field_mappings"]) == 2


# =============================================================================
# 6. test_analyze_feed_medium_confidence
# =============================================================================
@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_medium_confidence(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"

    _setup_openai_mock(mock_openai_class, json.dumps(MOCK_MEDIUM_RESPONSE))

    result = await analyze_feed(SAMPLE_ENTRIES)

    # 4 of 6 expected fields -> medium confidence (66%)
    assert result["confidence"] == "medium"
    assert "field_mappings" in result["field_mapping"]


# =============================================================================
# 7. test_analyze_feed_api_error
# =============================================================================
@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_api_error(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=Exception("API rate limit exceeded")
    )
    mock_openai_class.return_value = mock_client

    result = await analyze_feed(SAMPLE_ENTRIES)

    assert result["confidence"] == "low"
    assert result["field_mapping"] == {}
    assert result["sample_results"] == []


# =============================================================================
# 8. _extract_content — content extraction from various message shapes
# =============================================================================

def _make_message(**fields):
    """Build a message object. Unset attributes raise AttributeError (caught by getattr → default).

    Uses SimpleNamespace (not MagicMock) so unset attributes do not auto-create
    truthy MagicMock objects — this is critical for testing the fallback chain
    in _extract_content where we check `getattr(msg, 'reasoning_content', None) or ""`.
    """
    return SimpleNamespace(**fields)


def test_extract_content_regular_model():
    """Non-thinking model: content holds the answer."""
    msg = _make_message(content='{"answer": 42}')
    assert _extract_content(msg) == '{"answer": 42}'


def test_extract_content_thinking_model_content_preferred():
    """Thinking model with both content and reasoning: content wins."""
    msg = _make_message(
        content='{"answer": 42}',
        reasoning="Let me think about this...",
    )
    assert _extract_content(msg) == '{"answer": 42}'


def test_extract_content_thinking_model_empty_content():
    """Thinking model where content is None: fall back to reasoning."""
    msg = _make_message(content=None, reasoning='{"answer": 42}')
    assert _extract_content(msg) == '{"answer": 42}'


def test_extract_content_reasoning_content_alias():
    """Some SDKs/providers use `reasoning_content` instead of `reasoning`."""
    msg = _make_message(
        content=None,
        reasoning=None,
        reasoning_content='{"answer": 42}',
    )
    assert _extract_content(msg) == '{"answer": 42}'


def test_extract_content_reasoning_details_array():
    """Reasoning can arrive as a structured `reasoning_details` array."""
    msg = _make_message(
        content=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=[
            {"type": "reasoning.text", "text": '{"answer": 42}'},
        ],
    )
    assert _extract_content(msg) == '{"answer": 42}'


def test_extract_content_reasoning_details_summary():
    """`reasoning.summary` items in reasoning_details are also extracted."""
    msg = _make_message(
        content=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=[
            {"type": "reasoning.summary", "summary": "First thought"},
            {"type": "reasoning.summary", "summary": "Second thought"},
        ],
    )
    result = _extract_content(msg)
    assert "First thought" in result
    assert "Second thought" in result


def test_extract_content_all_empty():
    """No content or reasoning anywhere: returns empty string."""
    msg = _make_message(
        content=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=[],
    )
    assert _extract_content(msg) == ""


# =============================================================================
# 9. _extract_json_object / _parse_llm_json — robust JSON extraction
# =============================================================================

def test_extract_json_object_plain():
    assert _extract_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_json_object_embedded_in_prose():
    text = 'Let me analyze this. {"a": 1, "b": 2} That is my answer.'
    assert _extract_json_object(text) == '{"a": 1, "b": 2}'


def test_extract_json_object_with_nested_braces():
    text = 'prefix {"outer": {"inner": 1}, "x": 2} suffix'
    assert _extract_json_object(text) == '{"outer": {"inner": 1}, "x": 2}'


def test_extract_json_object_braces_inside_strings():
    """Braces inside JSON string values must not break depth tracking."""
    text = 'prefix {"desc": "a {nested} brace", "ok": true} suffix'
    result = _extract_json_object(text)
    assert result is not None
    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["desc"] == "a {nested} brace"


def test_extract_json_object_no_object():
    assert _extract_json_object("no json here") is None


def test_parse_llm_json_plain():
    assert _parse_llm_json('{"a": 1}') == {"a": 1}


def test_parse_llm_json_code_fence():
    assert _parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_llm_json_embedded_in_prose():
    """Reasoning models may wrap JSON in explanatory prose."""
    text = 'I analyzed the feed. Here is my mapping:\n{"a": 1, "b": 2}\nThat is the result.'
    assert _parse_llm_json(text) == {"a": 1, "b": 2}


def test_parse_llm_json_invalid_escapes():
    """LLMs emit regex with \\s, \\d, \\b which aren't valid JSON escapes."""
    text = r'{"regex": "\s+(\d+)"}'
    result = _parse_llm_json(text)
    assert result["regex"] == r"\s+(\d+)"


def test_parse_llm_json_mixed_valid_and_invalid_escapes():
    """A valid escape pair (e.g. ``\\n``) stays intact while an invalid one
    (``\\d``) is double-escaped — exercising the skip-two-characters branch."""
    text = r'{"regex": "\d+(\n)"}'
    result = _parse_llm_json(text)
    # \d is double-escaped (invalid), \n is a real escape → newline in value.
    assert result["regex"] == r"\d+(" + "\n" + ")"


def test_parse_llm_json_extracted_object_still_invalid_raises():
    """Balanced-object extraction found something, but it still isn't valid
    JSON (trailing comma) → the last-resort strategy fails and we raise."""
    with pytest.raises(json.JSONDecodeError):
        _parse_llm_json('{"a": 1,}')


def test_parse_llm_json_raises_on_garbage():
    with pytest.raises(json.JSONDecodeError):
        _parse_llm_json("not json at all {{{")


# =============================================================================
# 10. OpenRouter path — non-streaming (mocked native SDK)
# =============================================================================

def _make_chunk(content="", reasoning="", reasoning_content=""):
    """Build a mock streaming chunk with the given delta fields."""
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    delta = MagicMock()
    delta.content = content if content else None
    delta.reasoning = reasoning if reasoning else None
    delta.reasoning_content = reasoning_content if reasoning_content else None
    chunk.choices[0].delta = delta
    return chunk


def _make_async_stream(chunks):
    """Return an async generator over the given mock chunks."""
    async def _gen():
        for c in chunks:
            yield c
    return _gen()


def _setup_openrouter_mock(mock_openrouter_class, chunks):
    """Wire up `OpenRouter(...)` as an async context manager that streams chunks."""
    mock_client = MagicMock()
    mock_client.chat.send_async = AsyncMock(return_value=_make_async_stream(chunks))

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client
    mock_cm.__aexit__.return_value = None

    mock_openrouter_class.return_value = mock_cm
    return mock_client


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_openrouter_non_thinking(mock_settings, mock_openrouter_class):
    """OpenRouter path with a non-thinking model: content only, no reasoning."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"

    chunks = [_make_chunk(content=json.dumps(MOCK_LLM_RESPONSE))]
    mock_client = _setup_openrouter_mock(mock_openrouter_class, chunks)

    result = await analyze_feed(SAMPLE_ENTRIES)

    assert result["confidence"] == "high"
    assert "field_mappings" in result["field_mapping"]
    assert "torrent_url" in result["field_mapping"]["field_mappings"]
    # Verify NO reasoning param was sent (would shrink openrouter/free pool)
    call_kwargs = mock_client.chat.send_async.call_args.kwargs
    assert "reasoning" not in call_kwargs, f"reasoning param must not be sent: {call_kwargs}"


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_openrouter_thinking_model(mock_settings, mock_openrouter_class):
    """OpenRouter path with a thinking model: reasoning deltas then content delta."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"

    chunks = [
        _make_chunk(reasoning="Let me analyze this feed..."),
        _make_chunk(reasoning="The title format is [group] title / en - ep"),
        _make_chunk(content=json.dumps(MOCK_LLM_RESPONSE)),
    ]
    _setup_openrouter_mock(mock_openrouter_class, chunks)

    result = await analyze_feed(SAMPLE_ENTRIES)

    # Content must be preferred over reasoning; reasoning trace must not corrupt JSON
    assert result["confidence"] == "high"
    assert "field_mappings" in result["field_mapping"]
    assert "torrent_url" in result["field_mapping"]["field_mappings"]


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_openrouter_answer_only_in_reasoning(mock_settings, mock_openrouter_class):
    """Some free reasoning models leave content empty; answer is in reasoning."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"

    chunks = [_make_chunk(reasoning=json.dumps(MOCK_LLM_RESPONSE))]
    _setup_openrouter_mock(mock_openrouter_class, chunks)

    result = await analyze_feed(SAMPLE_ENTRIES)

    assert result["confidence"] == "high"
    assert "torrent_url" in result["field_mapping"]["field_mappings"]


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_openrouter_reasoning_content_alias(mock_settings, mock_openrouter_class):
    """The `reasoning_content` alias (used by some SDKs) must be handled."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"

    chunks = [_make_chunk(reasoning_content=json.dumps(MOCK_LLM_RESPONSE))]
    _setup_openrouter_mock(mock_openrouter_class, chunks)

    result = await analyze_feed(SAMPLE_ENTRIES)

    assert result["confidence"] == "high"
    assert "torrent_url" in result["field_mapping"]["field_mappings"]


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_openrouter_empty_response(mock_settings, mock_openrouter_class):
    """OpenRouter returns no content and no reasoning: result is low confidence."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"

    chunks = [_make_chunk(content="", reasoning="")]
    _setup_openrouter_mock(mock_openrouter_class, chunks)

    result = await analyze_feed(SAMPLE_ENTRIES)

    assert result["confidence"] == "low"
    assert result["field_mapping"] == {}


# =============================================================================
# 11. OpenRouter path — streaming (mocked native SDK)
# =============================================================================

async def _collect_events(gen):
    events = []
    async for event in gen:
        events.append(event)
    return events


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_stream_openrouter_non_thinking(mock_settings, mock_openrouter_class):
    """Streaming OpenRouter path with a non-thinking model."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"

    json_str = json.dumps(MOCK_LLM_RESPONSE)
    chunks = [
        _make_chunk(content=json_str[:30]),
        _make_chunk(content=json_str[30:]),
    ]
    mock_client = _setup_openrouter_mock(mock_openrouter_class, chunks)

    events = await _collect_events(analyze_feed_stream(SAMPLE_ENTRIES))

    deltas = [e for e in events if e["type"] == "delta"]
    done = [e for e in events if e["type"] == "done"]
    errors = [e for e in events if e["type"] == "error"]
    assert len(deltas) == 2
    assert len(errors) == 0
    assert len(done) == 1
    assert done[0]["confidence"] == "high"
    assert "torrent_url" in done[0]["field_mapping"]["field_mappings"]
    # Verify NO reasoning param was sent
    call_kwargs = mock_client.chat.send_async.call_args.kwargs
    assert "reasoning" not in call_kwargs


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_stream_openrouter_thinking(mock_settings, mock_openrouter_class):
    """Streaming OpenRouter path with a thinking model: reasoning then content."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"

    json_str = json.dumps(MOCK_LLM_RESPONSE)
    chunks = [
        _make_chunk(reasoning="Thinking about the regex..."),
        _make_chunk(content=json_str[:30]),
        _make_chunk(content=json_str[30:]),
    ]
    _setup_openrouter_mock(mock_openrouter_class, chunks)

    events = await _collect_events(analyze_feed_stream(SAMPLE_ENTRIES))

    deltas = [e for e in events if e["type"] == "delta"]
    done = [e for e in events if e["type"] == "done"]
    errors = [e for e in events if e["type"] == "error"]
    # Reasoning delta + 2 content deltas = 3 delta events
    assert len(deltas) == 3
    assert len(errors) == 0
    assert len(done) == 1
    assert done[0]["confidence"] == "high"
    assert "torrent_url" in done[0]["field_mapping"]["field_mappings"]


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_stream_openrouter_thinking_only_reasoning(mock_settings, mock_openrouter_class):
    """Streaming thinking model that emits answer only via reasoning."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"

    json_str = json.dumps(MOCK_LLM_RESPONSE)
    chunks = [_make_chunk(reasoning=json_str)]
    _setup_openrouter_mock(mock_openrouter_class, chunks)

    events = await _collect_events(analyze_feed_stream(SAMPLE_ENTRIES))

    done = [e for e in events if e["type"] == "done"]
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 0
    assert len(done) == 1
    assert done[0]["confidence"] == "high"
    assert "torrent_url" in done[0]["field_mapping"]["field_mappings"]


# =============================================================================
# 12. OpenAI path — no reasoning param (regression guard)
# =============================================================================

@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_openai_thinking_disabled_by_default(mock_settings, mock_openai_class):
    """OpenAI path sends enable_thinking=False by default for faster responses."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    mock_settings.llm_enable_thinking = False

    _setup_openai_mock(mock_openai_class, json.dumps(MOCK_LLM_RESPONSE))

    await analyze_feed(SAMPLE_ENTRIES)

    call_kwargs = mock_openai_class.return_value.chat.completions.create.call_args.kwargs
    assert call_kwargs.get("extra_body", {}).get("enable_thinking") is False, \
        f"enable_thinking must be False by default: {call_kwargs}"
    # reasoning must never be sent (breaks non-thinking models via extra_body)
    assert "reasoning" not in (call_kwargs.get("extra_body") or {}), \
        f"reasoning must not be sent via extra_body: {call_kwargs}"


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_call_openai_sends_both_thinking_spellings(mock_settings, mock_openai_class):
    """F6: extra_body carries enable_thinking top-level AND under
    chat_template_kwargs (sglang/vLLM only honor the latter)."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    mock_settings.llm_enable_thinking = False

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response(content='{"ok": true}')
    )
    mock_openai_class.return_value = mock_client

    await _call_openai([{"role": "user", "content": "test"}])

    extra_body = mock_client.chat.completions.create.call_args.kwargs["extra_body"]
    assert extra_body["enable_thinking"] is False
    assert extra_body["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_call_openai_thinking_enabled_propagates_to_both(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    mock_settings.llm_enable_thinking = True

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response(content='{"ok": true}')
    )
    mock_openai_class.return_value = mock_client

    await _call_openai([{"role": "user", "content": "test"}])

    extra_body = mock_client.chat.completions.create.call_args.kwargs["extra_body"]
    assert extra_body["enable_thinking"] is True
    assert extra_body["chat_template_kwargs"] == {"enable_thinking": True}


# =============================================================================
# 13. _call_openai — direct function tests
# =============================================================================

def _make_openai_response(content=None, reasoning=None, reasoning_content=None):
    """Build a mock AsyncOpenAI chat completion response."""
    msg = SimpleNamespace(
        content=content,
        reasoning=reasoning,
        reasoning_content=reasoning_content,
        reasoning_details=None,
    )
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_call_openai_non_thinking_model(mock_settings, mock_openai_class):
    """_call_openai returns content from a regular (non-thinking) model."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "gpt-4o"
    mock_settings.llm_enable_thinking = False

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response(content='{"ok": true}')
    )
    mock_openai_class.return_value = mock_client

    messages = [{"role": "user", "content": "test"}]
    result = await _call_openai(messages)

    assert result == '{"ok": true}'
    mock_client.chat.completions.create.assert_called_once()
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-4o"
    assert call_kwargs["messages"] == messages
    assert call_kwargs["temperature"] == 0.1


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_call_openai_thinking_model_content_empty(mock_settings, mock_openai_class):
    """_call_openai falls back to reasoning_content when content is empty."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "glm-thinking"

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response(content=None, reasoning_content='{"answer": 42}')
    )
    mock_openai_class.return_value = mock_client

    result = await _call_openai([{"role": "user", "content": "test"}])
    assert result == '{"answer": 42}'


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_call_openai_constructs_client_with_correct_params(mock_settings, mock_openai_class):
    """_call_openai passes api_key, base_url, and timeout to AsyncOpenAI constructor."""
    mock_settings.llm_api_key = "sk-secret"
    mock_settings.llm_base_url = "https://custom.api/v1"
    mock_settings.llm_model = "test-model"

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_response(content="hello")
    )
    mock_openai_class.return_value = mock_client

    await _call_openai([{"role": "user", "content": "hi"}])

    mock_openai_class.assert_called_once()
    ctor_kwargs = mock_openai_class.call_args.kwargs
    assert ctor_kwargs["api_key"] == "sk-secret"
    assert ctor_kwargs["base_url"] == "https://custom.api/v1"
    assert "timeout" in ctor_kwargs


# =============================================================================
# 14. _stream_openai — streaming path direct tests
# =============================================================================

def _make_stream_chunk(content=None, reasoning=None, reasoning_content=None):
    """Build a mock streaming chunk (OpenAI SSE delta)."""
    delta = SimpleNamespace(
        content=content,
        reasoning=reasoning,
        reasoning_content=reasoning_content,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _make_openai_stream(chunks):
    async def _gen():
        for c in chunks:
            yield c
    return _gen()


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_stream_openai_non_thinking_model(mock_settings, mock_openai_class):
    """_stream_openai yields delta events then a done event for a regular model."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "gpt-4o"

    json_str = json.dumps(MOCK_LLM_RESPONSE)
    chunks = [
        _make_stream_chunk(content=json_str[:20]),
        _make_stream_chunk(content=json_str[20:]),
    ]
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_make_openai_stream(chunks))
    mock_openai_class.return_value = mock_client

    events = await _collect_events(
        _stream_openai([{"role": "user", "content": "analyze"}])
    )

    deltas = [e for e in events if e["type"] == "delta"]
    done = [e for e in events if e["type"] == "done"]
    errors = [e for e in events if e["type"] == "error"]

    assert len(deltas) == 2
    assert len(errors) == 0
    assert len(done) == 1
    assert done[0]["confidence"] == "high"
    assert "torrent_url" in done[0]["field_mapping"]["field_mappings"]

    # Verify stream=True was passed
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["stream"] is True
    assert call_kwargs["model"] == "gpt-4o"


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_stream_openai_thinking_model_reasoning_content(mock_settings, mock_openai_class):
    """_stream_openai handles thinking model deltas (reasoning_content) then content."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "glm-z1"

    json_str = json.dumps(MOCK_LLM_RESPONSE)
    chunks = [
        _make_stream_chunk(reasoning_content="Thinking about the regex patterns..."),
        _make_stream_chunk(content=json_str),
    ]
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_make_openai_stream(chunks))
    mock_openai_class.return_value = mock_client

    events = await _collect_events(
        _stream_openai([{"role": "user", "content": "analyze"}])
    )

    deltas = [e for e in events if e["type"] == "delta"]
    done = [e for e in events if e["type"] == "done"]
    errors = [e for e in events if e["type"] == "error"]

    # reasoning delta + 1 content delta = 2
    assert len(deltas) == 2
    assert len(errors) == 0
    assert len(done) == 1
    assert done[0]["confidence"] == "high"


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_stream_openai_empty_response(mock_settings, mock_openai_class):
    """_stream_openai yields an error event when LLM returns no content."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"

    chunks = [_make_stream_chunk(content="")]
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_make_openai_stream(chunks))
    mock_openai_class.return_value = mock_client

    events = await _collect_events(
        _stream_openai([{"role": "user", "content": "analyze"}])
    )

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert "empty" in errors[0]["message"].lower()


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_stream_openai_invalid_json(mock_settings, mock_openai_class):
    """_stream_openai yields an error event when the LLM response is not parseable JSON."""
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"

    chunks = [_make_stream_chunk(content="Sorry, I cannot help with that.")]
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_make_openai_stream(chunks))
    mock_openai_class.return_value = mock_client

    events = await _collect_events(
        _stream_openai([{"role": "user", "content": "analyze"}])
    )

    # The function might yield a delta and then an error — verify at least one error
    errors = [e for e in events if e["type"] == "error"]
    done = [e for e in events if e["type"] == "done"]
    assert len(errors) >= 1
    assert len(done) == 0


# =============================================================================
# 15. call_llm — provider routing
# =============================================================================

@pytest.mark.asyncio
@patch("app.services.feed_analyzer._call_openai")
@patch("app.services.feed_analyzer._call_openrouter")
@patch("app.services.runtime_config.settings")
async def test_call_llm_routes_to_openai_for_non_openrouter(mock_settings, mock_openrouter, mock_openai):
    """call_llm uses _call_openai when llm_base_url is not OpenRouter."""
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://api.test.com/v1"
    mock_settings.llm_model = "gpt-4o"
    mock_openai.return_value = "response"

    messages = [{"role": "user", "content": "hi"}]
    result = await call_llm(messages)

    mock_openai.assert_called_once_with(messages)
    mock_openrouter.assert_not_called()
    assert result == "response"


@pytest.mark.asyncio
@patch("app.services.feed_analyzer._call_openai")
@patch("app.services.feed_analyzer._call_openrouter")
@patch("app.services.runtime_config.settings")
async def test_call_llm_routes_to_openrouter(mock_settings, mock_openrouter, mock_openai):
    """call_llm uses _call_openrouter when llm_base_url contains 'openrouter'."""
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"
    mock_openrouter.return_value = "response"

    messages = [{"role": "user", "content": "hi"}]
    result = await call_llm(messages)

    mock_openrouter.assert_called_once_with(messages)
    mock_openai.assert_not_called()
    assert result == "response"


# =============================================================================
# 16. Regression: AsyncOpenAI constructor must not raise TypeError for proxies
#     (openai==1.51.0 + httpx>=0.28 was incompatible; fixed in openai>=1.52.0)
# =============================================================================

def test_async_openai_constructor_no_proxies_error():
    """AsyncOpenAI construction must not raise TypeError about 'proxies'."""
    import httpx
    from openai import AsyncOpenAI as _AsyncOpenAI

    try:
        _AsyncOpenAI(
            api_key="test",
            base_url="https://example.com/v1",
            timeout=httpx.Timeout(10.0),
        )
    except TypeError as e:
        pytest.fail(f"AsyncOpenAI raised TypeError (proxies incompatibility?): {e}")


# =============================================================================
# 17. analyze_feed — guard clauses (no entries / no api key)
# =============================================================================


@pytest.mark.asyncio
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_no_entries(mock_settings):
    mock_settings.llm_api_key = "test-key"
    result = await analyze_feed([])
    assert result["confidence"] == "low"
    assert result["field_mapping"] == {}


# =============================================================================
# 18. analyze_feed — daily-limit / per-minute-limit / per-day retry branches
# =============================================================================


def _raise_on(exc: Exception):
    """Return a callable that raises ``exc`` every time."""
    async def _f(*a, **kw):
        raise exc
    return _f


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_per_day_limit_returns_immediately(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=Exception("daily tokens per-day limit exceeded")
    )
    mock_openai_class.return_value = mock_client

    result = await analyze_feed(SAMPLE_ENTRIES)
    assert result["confidence"] == "low"
    assert result["field_mapping"] == {}
    # Daily limit is not retryable → exactly one call, no 65s sleeps.
    assert mock_client.chat.completions.create.await_count == 1


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_per_minute_limit_retries_then_succeeds(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=[
            Exception("per-minute rate limit reached"),
            _make_openai_response(content=json.dumps(MOCK_LLM_RESPONSE)),
        ]
    )
    mock_openai_class.return_value = mock_client

    result = await analyze_feed(SAMPLE_ENTRIES)
    assert result["confidence"] == "high"
    assert mock_client.chat.completions.create.await_count == 2


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_per_minute_limit_exhausts_all_attempts(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=Exception("per-minute rate limit exceeded")
    )
    mock_openai_class.return_value = mock_client

    result = await analyze_feed(SAMPLE_ENTRIES)
    assert result["confidence"] == "low"
    assert result["field_mapping"] == {}
    assert mock_client.chat.completions.create.await_count == 3


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_retries_on_empty_then_succeeds(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=[
            _make_openai_response(content="   "),
            _make_openai_response(content=json.dumps(MOCK_LLM_RESPONSE)),
        ]
    )
    mock_openai_class.return_value = mock_client

    result = await analyze_feed(SAMPLE_ENTRIES)
    assert result["confidence"] == "high"
    assert mock_client.chat.completions.create.await_count == 2


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_all_attempts_fail_generic(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "test-key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=Exception("upstream 500")
    )
    mock_openai_class.return_value = mock_client

    result = await analyze_feed(SAMPLE_ENTRIES)
    assert result["confidence"] == "low"
    assert result["field_mapping"] == {}
    assert mock_client.chat.completions.create.await_count == 3


# =============================================================================
# 19. _extract_content — reasoning_details with object (non-dict) items
# =============================================================================


def test_extract_content_reasoning_details_object_items():
    msg = SimpleNamespace(
        content=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=[
            SimpleNamespace(summary="First", text="ignored"),
            SimpleNamespace(summary=None, text="Second"),
        ],
    )
    result = _extract_content(msg)
    assert "First" in result
    assert "Second" in result


# =============================================================================
# 20. _parse_llm_json / _extract_json_object edge cases
# =============================================================================


def test_parse_llm_json_unterminated_fence_raises():
    # A fence opener without a closing fence leaves garbage behind → not JSON.
    with pytest.raises(json.JSONDecodeError):
        _parse_llm_json("```json")


def test_extract_json_object_handles_escaped_quotes_inside_strings():
    # ``\"`` keeps the string open; the brace depth must not be thrown off.
    text = 'pre {"a": "say \\"hi\\"", "b": 2} post'
    result = _extract_json_object(text)
    assert result is not None
    parsed = json.loads(result)
    assert parsed["b"] == 2
    assert parsed["a"] == 'say "hi"'


def test_extract_json_object_unescaped_quote_inside_string_closes_string():
    text = 'pre {"a": "has "quote" inside", "b": 2}'
    result = _extract_json_object(text)
    assert result is not None


# =============================================================================
# 21. _call_openrouter — direct function tests (native SDK)
# =============================================================================


def _setup_openrouter_client(chunks):
    client = MagicMock()
    client.chat.send_async = AsyncMock(return_value=_make_async_stream(chunks))
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = None
    return cm, client


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_call_openrouter_prefers_content_over_reasoning(mock_settings, mock_openrouter_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"
    cm, _ = _setup_openrouter_client([
        _make_chunk(reasoning="thinking trace"),
        _make_chunk(content="the-answer"),
    ])
    mock_openrouter_class.return_value = cm

    result = await _call_openrouter([{"role": "user", "content": "hi"}])
    assert result == "the-answer"


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_call_openrouter_falls_back_to_reasoning_when_content_empty(mock_settings, mock_openrouter_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"
    cm, _ = _setup_openrouter_client([_make_chunk(reasoning="the-answer")])
    mock_openrouter_class.return_value = cm

    result = await _call_openrouter([{"role": "user", "content": "hi"}])
    assert result == "the-answer"


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_call_openrouter_skips_chunks_without_choices(mock_settings, mock_openrouter_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"
    empty_chunk = MagicMock()
    empty_chunk.choices = []
    cm, _ = _setup_openrouter_client([
        empty_chunk,
        _make_chunk(content="the-answer"),
    ])
    mock_openrouter_class.return_value = cm

    result = await _call_openrouter([{"role": "user", "content": "hi"}])
    assert result == "the-answer"


# =============================================================================
# 22. analyze_feed_stream — guard clauses
# =============================================================================


@pytest.mark.asyncio
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_stream_no_entries(mock_settings):
    mock_settings.llm_api_key = "test-key"
    events = await _collect_events(analyze_feed_stream([]))
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "No entries" in events[0]["message"]


@pytest.mark.asyncio
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_stream_no_api_key(mock_settings):
    mock_settings.llm_api_key = ""
    events = await _collect_events(analyze_feed_stream(SAMPLE_ENTRIES))
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "API key" in events[0]["message"]


# =============================================================================
# 23. analyze_feed_stream — provider routing + exception surface
# =============================================================================


@pytest.mark.asyncio
@patch("app.services.feed_analyzer._stream_openai")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_stream_routes_to_openai(mock_settings, mock_stream_openai):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_stream_openai.return_value = _make_async_stream([
        {"type": "done", "field_mapping": {"x": 1}, "confidence": "high"},
    ])
    events = await _collect_events(analyze_feed_stream(SAMPLE_ENTRIES))
    mock_stream_openai.assert_called_once()
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
@patch("app.services.feed_analyzer._stream_openrouter")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_stream_routes_to_openrouter(mock_settings, mock_stream_openrouter):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_stream_openrouter.return_value = _make_async_stream([
        {"type": "done", "field_mapping": {"x": 1}, "confidence": "high"},
    ])
    events = await _collect_events(analyze_feed_stream(SAMPLE_ENTRIES))
    mock_stream_openrouter.assert_called_once()
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
@patch("app.services.feed_analyzer._stream_openai")
@patch("app.services.runtime_config.settings")
async def test_analyze_feed_stream_surfaces_stream_exception(mock_settings, mock_stream_openai):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://api.test.com"
    async def _boom(messages):
        raise RuntimeError("stream exploded")
        yield  # pragma: no cover
    mock_stream_openai.side_effect = _boom
    events = await _collect_events(analyze_feed_stream(SAMPLE_ENTRIES))
    assert events[-1]["type"] == "error"
    assert "stream exploded" in events[-1]["message"]


# =============================================================================
# 24. _stream_openrouter — retries, reset event, per-day branch, empty, invalid
# =============================================================================


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_stream_openrouter_per_minute_rate_limit_retries_then_succeeds(mock_settings, mock_openrouter_class):
    """A per-minute rate-limit error on attempt 1 waits 65s then succeeds."""
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"
    client = MagicMock()
    client.chat.send_async = AsyncMock(side_effect=[
        RuntimeError("per-minute rate limit reached"),
        _make_async_stream([_make_chunk(content=json.dumps(MOCK_LLM_RESPONSE))]),
    ])
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = None
    mock_openrouter_class.return_value = cm

    events = await _collect_events(_stream_openrouter([{"role": "user", "content": "hi"}]))
    assert events[-1]["type"] == "done"
    assert events[-1]["confidence"] == "high"
    assert client.chat.send_async.await_count == 2


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_stream_openrouter_retries_transient_then_succeeds(mock_settings, mock_openrouter_class):
    """A network failure on attempt 1 → reset event → success on attempt 2."""
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"
    client = MagicMock()
    client.chat.send_async = AsyncMock(side_effect=[
        RuntimeError("connection reset"),
        _make_async_stream([_make_chunk(content=json.dumps(MOCK_LLM_RESPONSE))]),
    ])
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = None
    mock_openrouter_class.return_value = cm

    events = await _collect_events(_stream_openrouter([{"role": "user", "content": "hi"}]))
    types = [e["type"] for e in events]
    assert types[0] == "reset"
    assert "done" in types


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_stream_openrouter_per_day_aborts_immediately(mock_settings, mock_openrouter_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"
    client = MagicMock()
    client.chat.send_async = AsyncMock(side_effect=RuntimeError("per-day usage limit reached"))
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = None
    mock_openrouter_class.return_value = cm

    events = await _collect_events(_stream_openrouter([{"role": "user", "content": "hi"}]))
    assert events[0]["type"] == "error"
    assert "per-day" in events[0]["message"]
    # Non-retryable → single attempt, no reset event.
    assert client.chat.send_async.await_count == 1


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_stream_openrouter_exhausts_transient_retries(mock_settings, mock_openrouter_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"
    client = MagicMock()
    client.chat.send_async = AsyncMock(side_effect=RuntimeError("network timeout"))
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = None
    mock_openrouter_class.return_value = cm

    events = await _collect_events(_stream_openrouter([{"role": "user", "content": "hi"}]))
    assert events[-1]["type"] == "error"
    assert client.chat.send_async.await_count == 3


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_stream_openrouter_empty_response_exhausts_retries(mock_settings, mock_openrouter_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"
    client = MagicMock()
    client.chat.send_async = AsyncMock(
        return_value=_make_async_stream([_make_chunk(content="", reasoning="")])
    )
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = None
    mock_openrouter_class.return_value = cm

    events = await _collect_events(_stream_openrouter([{"role": "user", "content": "hi"}]))
    assert events[-1]["type"] == "error"
    assert "empty" in events[-1]["message"].lower()
    assert client.chat.send_async.await_count == 3


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_stream_openrouter_invalid_json_exhausts_retries(mock_settings, mock_openrouter_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"
    client = MagicMock()
    # Fresh stream on every attempt (a reused async generator would be
    # exhausted after the first parse failure → empty response).
    client.chat.send_async = AsyncMock(
        side_effect=lambda *a, **kw: _make_async_stream([_make_chunk(content="not json {{{")])
    )
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = None
    mock_openrouter_class.return_value = cm

    events = await _collect_events(_stream_openrouter([{"role": "user", "content": "hi"}]))
    assert events[-1]["type"] == "error"
    assert "invalid JSON" in events[-1]["message"]
    assert client.chat.send_async.await_count == 3


@pytest.mark.asyncio
@patch("openrouter.OpenRouter")
@patch("app.services.runtime_config.settings")
async def test_stream_openrouter_skips_chunks_without_choices(mock_settings, mock_openrouter_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://openrouter.ai/api/v1"
    mock_settings.llm_model = "openrouter/free"
    empty_chunk = MagicMock()
    empty_chunk.choices = []
    client = MagicMock()
    client.chat.send_async = AsyncMock(return_value=_make_async_stream([
        empty_chunk,
        _make_chunk(content=json.dumps(MOCK_LLM_RESPONSE)),
    ]))
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = None
    mock_openrouter_class.return_value = cm

    events = await _collect_events(_stream_openrouter([{"role": "user", "content": "hi"}]))
    assert events[-1]["type"] == "done"


# =============================================================================
# 25. _stream_openai — exception branch / skip-no-choices / invalid JSON exhaust
# =============================================================================


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_stream_openai_exhausts_transient_retries(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))
    mock_openai_class.return_value = mock_client

    events = await _collect_events(_stream_openai([{"role": "user", "content": "hi"}]))
    assert events[-1]["type"] == "error"
    assert mock_client.chat.completions.create.await_count == 3


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_stream_openai_skips_chunks_without_choices(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    empty_chunk = SimpleNamespace(choices=[])
    content_chunk = _make_stream_chunk(content=json.dumps(MOCK_LLM_RESPONSE))
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_make_openai_stream([
        empty_chunk, content_chunk,
    ]))
    mock_openai_class.return_value = mock_client

    events = await _collect_events(_stream_openai([{"role": "user", "content": "hi"}]))
    assert events[-1]["type"] == "done"
    assert events[-1]["confidence"] == "high"


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_stream_openai_invalid_json_exhausts_retries(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=lambda *a, **kw: _make_openai_stream([_make_stream_chunk(content="nope {{{")])
    )
    mock_openai_class.return_value = mock_client

    events = await _collect_events(_stream_openai([{"role": "user", "content": "hi"}]))
    assert events[-1]["type"] == "error"
    assert "invalid JSON" in events[-1]["message"]
    assert mock_client.chat.completions.create.await_count == 3


@pytest.mark.asyncio
@patch("app.services.feed_analyzer.AsyncOpenAI")
@patch("app.services.runtime_config.settings")
async def test_stream_openai_empty_response_exhausts_retries(mock_settings, mock_openai_class):
    mock_settings.llm_api_key = "key"
    mock_settings.llm_base_url = "https://api.test.com"
    mock_settings.llm_model = "test-model"
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_make_openai_stream([_make_stream_chunk(content="")])
    )
    mock_openai_class.return_value = mock_client

    events = await _collect_events(_stream_openai([{"role": "user", "content": "hi"}]))
    assert events[-1]["type"] == "error"
    assert "empty" in events[-1]["message"].lower()
    assert mock_client.chat.completions.create.await_count == 3
