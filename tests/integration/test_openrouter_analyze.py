"""Integration tests for OpenRouter ``openrouter/free`` LLM analysis.

These tests hit the **real** OpenRouter API using the native OpenRouter SDK
and the ``openrouter/free`` router slug, which randomly selects a free model
on each request.

Design principle: the **entire module makes exactly 2 LLM API calls** —
one non-streaming and one streaming — which are cached as session fixtures.
All tests verify different aspects of those shared results. This keeps the
test suite within the openrouter/free per-minute and per-day rate limits.

Requirements:
    - ``LLM_API_KEY`` env var (loaded from ``.env``) — tests skip otherwise.

Run separately::

    uv run pytest tests/integration/test_openrouter_analyze.py -v
"""

import asyncio
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Load .env from project root before evaluating skipif
_project_root = Path(__file__).resolve().parents[2]
load_dotenv(_project_root / ".env")

from app.services.feed_analyzer import (  # noqa: E402  (import after load_dotenv)
    _is_openrouter,
    analyze_feed,
    analyze_feed_stream,
)

# Skip the entire module unless an API key is set AND the provider is OpenRouter.
pytestmark = pytest.mark.skipif(
    not os.getenv("LLM_API_KEY") or not _is_openrouter(),
    reason="LLM_API_KEY not set or LLM_BASE_URL is not OpenRouter — skipping OpenRouter integration tests",
)


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Override the integration conftest's autouse fixture.

    These tests talk directly to OpenRouter and do not need the local
    test-server (mock RSS feeds / tracker) to be running.
    """
    pass


# ---------------------------------------------------------------------------
# Sample entries — representative Mikanani anime RSS format
# ---------------------------------------------------------------------------

SAMPLE_ENTRIES_MIKANANI = [
    {
        "title": "[LoliHouse] 间谍过家家 / Spy x Family - 12 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]",
        "enclosures": [{"url": "https://mikanani.me/Download/2024/test1.torrent", "length": "524288000"}],
        "link": "https://mikanani.me/Home/Episode/abc123",
        "description": "LoliHouse 出品",
        "published": "2024-07-15T08:00:00+08:00",
    },
    {
        "title": "[LoliHouse] 间谍过家家 / Spy x Family - 13 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]",
        "enclosures": [{"url": "https://mikanani.me/Download/2024/test2.torrent", "length": "536870912"}],
        "link": "https://mikanani.me/Home/Episode/def456",
        "description": "LoliHouse 出品",
        "published": "2024-07-22T08:00:00+08:00",
    },
]


# ---------------------------------------------------------------------------
# Session-scoped fixtures — ONE LLM call each, shared across all tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def non_streaming_result():
    """Run analyze_feed ONCE and cache the result for all non-streaming tests."""
    result = asyncio.run(analyze_feed(SAMPLE_ENTRIES_MIKANANI))
    if result["confidence"] == "low" and not result["field_mapping"]:
        pytest.skip(
            "OpenRouter returned empty mapping (rate limit or provider error). "
            "Re-run when quota resets."
        )
    return result


@pytest.fixture(scope="session")
def streaming_events():
    """Run analyze_feed_stream ONCE and collect all events for stream tests."""
    events: list[dict] = []

    async def _collect():
        async for event in analyze_feed_stream(SAMPLE_ENTRIES_MIKANANI):
            events.append(event)

    asyncio.run(_collect())
    if not any(e["type"] == "done" for e in events):
        pytest.skip(
            "OpenRouter streaming returned no done event (rate limit or provider error). "
            "Re-run when quota resets."
        )
    return events


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def test_is_openrouter_detection():
    """``_is_openrouter()`` must detect the configured OpenRouter base URL."""
    assert _is_openrouter() is True, (
        "Expected _is_openrouter() to be True — check LLM_BASE_URL in .env"
    )


# ---------------------------------------------------------------------------
# Non-streaming analysis — all share one API call via session fixture
# ---------------------------------------------------------------------------

def test_analyze_feed_has_confidence(non_streaming_result):
    """Result has confidence key and it is not 'low'."""
    assert "confidence" in non_streaming_result
    assert non_streaming_result["confidence"] != "low", (
        f"confidence was 'low': {non_streaming_result}"
    )


def test_analyze_feed_mapping_structure(non_streaming_result):
    """field_mapping has required list_locator and field_mappings keys."""
    fm = non_streaming_result["field_mapping"]
    assert "list_locator" in fm
    assert "field_mappings" in fm


def test_analyze_feed_has_torrent_url(non_streaming_result):
    """field_mappings includes torrent_url — required for download."""
    mappings = non_streaming_result["field_mapping"]["field_mappings"]
    assert "torrent_url" in mappings, (
        f"torrent_url missing from mappings: {list(mappings)}"
    )


def test_analyze_feed_has_title_field(non_streaming_result):
    """field_mappings includes at least title_cn or title_en."""
    mappings = non_streaming_result["field_mapping"]["field_mappings"]
    assert "title_cn" in mappings or "title_en" in mappings, (
        f"Neither title_cn nor title_en in mappings: {list(mappings)}"
    )


def test_analyze_feed_all_rules_have_source(non_streaming_result):
    """Every mapping rule must have a 'source' key."""
    mappings = non_streaming_result["field_mapping"]["field_mappings"]
    for field_name, rule in mappings.items():
        assert "source" in rule, f"rule for {field_name!r} missing 'source': {rule}"


def test_generated_mapping_extracts_fields(non_streaming_result):
    """The LLM-generated mapping must actually extract torrent_url from entries."""
    from app.services.resource_parser import parse_entry

    field_mapping = non_streaming_result["field_mapping"]
    for entry in SAMPLE_ENTRIES_MIKANANI:
        extracted = parse_entry(entry, field_mapping)
        assert extracted.get("torrent_url"), (
            f"torrent_url not extracted from entry {entry['title']!r}: {extracted}"
        )


# ---------------------------------------------------------------------------
# Edge cases — these don't make LLM calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_feed_empty_entries():
    """analyze_feed([]) must return a valid low-confidence empty result."""
    result = await analyze_feed([])
    assert "field_mapping" in result
    assert "confidence" in result
    assert result["confidence"] == "low"
    assert result["field_mapping"] == {}


# ---------------------------------------------------------------------------
# Streaming analysis — one API call via session fixture
# ---------------------------------------------------------------------------

def test_analyze_feed_streaming_has_deltas(streaming_events):
    """Streaming must emit at least one delta event."""
    deltas = [e for e in streaming_events if e["type"] == "delta"]
    assert len(deltas) > 0, "Expected at least one delta event"


def test_analyze_feed_streaming_no_errors(streaming_events):
    """Streaming must not emit error events."""
    error_events = [e for e in streaming_events if e["type"] == "error"]
    assert len(error_events) == 0, (
        f"Got error: {error_events[0].get('message') if error_events else 'n/a'}"
    )


def test_analyze_feed_streaming_done_event(streaming_events):
    """Streaming must emit exactly one done event with a valid field_mapping."""
    done_events = [e for e in streaming_events if e["type"] == "done"]
    assert len(done_events) == 1, f"Expected exactly one done event, got {len(done_events)}"
    done = done_events[0]
    assert "field_mapping" in done
    assert "list_locator" in done["field_mapping"]
    assert "field_mappings" in done["field_mapping"]
    assert "confidence" in done
    assert done["confidence"] != "low", f"Streaming confidence was 'low': {done}"
