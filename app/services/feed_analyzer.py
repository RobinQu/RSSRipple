"""LLM-based RSS feed analysis service.

Uses an OpenAI-compatible LLM to analyze sample RSS entries and generate
per-channel field mappings for the dynamic resource parser.

Supports both OpenRouter SDK (for reasoning model support) and OpenAI SDK
(for other providers).
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

import httpx
from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _is_openrouter() -> bool:
    """Check if the configured LLM base URL is OpenRouter."""
    return "openrouter" in (settings.llm_base_url or "").lower()


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

_VALID_JSON_ESCAPES = frozenset('"\\/bfnrtu')


def _fix_invalid_escapes(text: str) -> str:
    """Double backslashes that aren't valid JSON escapes inside strings."""
    result = []
    i = 0
    in_string = False
    while i < len(text):
        ch = text[i]
        if ch == '"' and (i == 0 or text[i - 1] != '\\'):
            in_string = not in_string
            result.append(ch)
        elif ch == '\\' and in_string and i + 1 < len(text):
            next_ch = text[i + 1]
            if next_ch in _VALID_JSON_ESCAPES:
                result.append(ch)
            else:
                result.append('\\\\')
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def _extract_json_object(text: str) -> str | None:
    """Extract the first balanced ``{...}`` JSON object from arbitrary text.

    Useful for reasoning (thinking) models that wrap their JSON output in
    explanatory prose. Scans for the first ``{`` and tracks brace depth while
    respecting string literals and escape sequences.

    Returns the substring of the first balanced object, or ``None`` if no
    complete object is found.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_llm_json(text: str) -> dict:
    r"""Parse JSON from LLM output, handling common issues.

    Handles:
    - Markdown code fences (```json ... ```)
    - Invalid escape sequences (\s, \d from regex)
    - Leading/trailing whitespace
    - JSON embedded in reasoning prose (thinking models)
    """
    text = text.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        first_nl = text.index("\n") if "\n" in text else 3
        text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # Try parsing directly first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fix invalid backslash escapes in JSON strings
    # (LLMs emit regex with \s, \d, \b etc. which aren't valid JSON escapes)
    fixed = _fix_invalid_escapes(text)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # Try with strict=False for control characters
    try:
        return json.loads(fixed, strict=False)
    except json.JSONDecodeError:
        pass

    # Last resort: extract the first balanced JSON object. Reasoning (thinking)
    # models may wrap their JSON output in explanatory prose.
    json_str = _extract_json_object(fixed)
    if json_str:
        try:
            return json.loads(json_str, strict=False)
        except json.JSONDecodeError:
            pass

    # All strategies failed — raise so the caller can handle gracefully
    raise json.JSONDecodeError("Could not parse JSON from LLM output", text, 0)


# ---------------------------------------------------------------------------
# Response content extraction (handles reasoning models)
# ---------------------------------------------------------------------------

def _extract_content(message) -> str:
    """Extract the usable content from an LLM response message.

    Handles both regular and reasoning (thinking) models:

    - **Regular models**: ``content`` holds the answer.
    - **Reasoning models**: ``content`` may be null/empty; the answer can
      appear in ``reasoning``, ``reasoning_content`` (alias used by some
      providers/SDKs), or ``reasoning_details`` (a structured array whose
      items carry ``reasoning.text`` or ``reasoning.summary`` payloads).

    The final answer is always preferred from ``content``; the reasoning
    fields are only consulted as a fallback when ``content`` is empty — this
    is the case for some free reasoning models routed via ``openrouter/free``
    that emit the answer only in the reasoning trace.
    """
    # Primary: content holds the final answer
    content = getattr(message, "content", None) or ""
    if content.strip():
        return content

    # Fallback 1: reasoning_content (alias used by some providers/SDKs)
    reasoning = getattr(message, "reasoning_content", None) or ""
    if reasoning.strip():
        return reasoning

    # Fallback 2: reasoning (plain string)
    reasoning = getattr(message, "reasoning", None) or ""
    if reasoning.strip():
        return reasoning

    # Fallback 3: reasoning_details (structured array)
    details = getattr(message, "reasoning_details", None) or []
    parts: list[str] = []
    for detail in details:
        if isinstance(detail, dict):
            text = detail.get("summary") or detail.get("text") or ""
        else:
            text = getattr(detail, "summary", None) or getattr(detail, "text", None) or ""
        if text:
            parts.append(text)
    if parts:
        return "\n".join(parts)

    return ""


# ---------------------------------------------------------------------------
# Validation and confidence helpers
# ---------------------------------------------------------------------------

def _validate_mapping(raw_mapping: dict) -> dict:
    """Validate and normalize a raw field mapping from the LLM."""
    if "field_mappings" in raw_mapping and isinstance(raw_mapping["field_mappings"], dict):
        inner_mappings = raw_mapping["field_mappings"]
        validated_inner = {}
        for field_name, rule in inner_mappings.items():
            if isinstance(rule, dict) and "source" in rule:
                validated_inner[field_name] = rule
        return {
            "list_locator": raw_mapping.get("list_locator", {"source": "entries"}),
            "field_mappings": validated_inner,
        }
    else:
        # Old flat format: wrap it
        validated_inner = {}
        for field_name, rule in raw_mapping.items():
            if isinstance(rule, dict) and "source" in rule:
                validated_inner[field_name] = rule
        return {
            "list_locator": {"source": "entries"},
            "field_mappings": validated_inner,
        }


def _calc_confidence(validated_mapping: dict) -> str:
    """Calculate confidence based on metadata field coverage.

    torrent_url is a required functional field, not a metadata quality indicator,
    so it is excluded here.  Having title_cn/title_en, subtitle_group, episode,
    resolution, and source reflects how completely the LLM understood the feed.
    """
    expected_fields = {
        "title_cn", "title_en", "subtitle_group", "episode",
        "resolution", "source",
    }
    field_mappings = validated_mapping.get("field_mappings", {})
    covered = set(field_mappings.keys()) & expected_fields
    ratio = len(covered) / len(expected_fields) if expected_fields else 0
    if ratio >= 0.8:
        return "high"
    elif ratio >= 0.5:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM_PROMPT = """You are an RSS feed analyzer. Given sample RSS entry data (in JSON format),
generate field mapping rules that extract structured fields for a download resource manager.

The output must be a JSON object with two parts:
1. "list_locator": describes where the list of entries is found in the feed (always "entries" for feedparser)
2. "field_mappings": a dict of per-field extraction rules

The target fields to extract are:
- title_cn (str): Chinese title/name
- title_en (str): English title/name
- subtitle_group (str): Release group name (often in brackets at the start)
- episode (int): Episode number
- season (int): Season number
- resolution (str): Video resolution (e.g., "1080p", "720p")
- source (str): Source type (e.g., "WebRip", "WEB-DL", "BDRip")
- video_codec (str): Video codec (e.g., "HEVC", "AVC", "H264")
- audio_codec (str): Audio codec (e.g., "AAC", "FLAC")
- subtitle_type (str): Subtitle type (e.g., "CHS", "CHT", "简繁")
- container (str): Container format (e.g., "MP4", "MKV")
- file_size (int): File size in bytes
- torrent_url (str): Download URL for the .torrent file
- detail_url (str): Detail page URL
- published_at (datetime): Publication date/time

Each field mapping rule has this structure:
{
  "source": "<feedparser entry field path>",
  "regex": "<optional regex pattern>",
  "group": <capture group index, default 0>,
  "transform": "<optional: int, float, iso_datetime, lowercase, uppercase>"
}

The "source" field is a dotted path into the feedparser entry object:
- "title" -> entry.title
- "enclosures[0].url" -> first enclosure's URL
- "description" -> entry description

Example output for a typical anime RSS feed:
{
  "list_locator": {"source": "entries"},
  "field_mappings": {
    "title_cn": {"source": "title", "regex": "\\\\]\\\\s*(.+?)\\\\s*/", "group": 1},
    "title_en": {"source": "title", "regex": "/\\\\s*(.+?)\\\\s*-", "group": 1},
    "subtitle_group": {"source": "title", "regex": "^\\\\[([^\\\\]]+)\\\\]", "group": 1},
    "episode": {"source": "title", "regex": "-\\\\s*(\\\\d+)\\\\b", "group": 1, "transform": "int"},
    "resolution": {"source": "title", "regex": "\\\\b(1080p|720p|480p|2160p|4K)\\\\b", "group": 1, "transform": "lowercase"},
    "torrent_url": {"source": "enclosures[0].url"},
    "file_size": {"source": "enclosures[0].length", "transform": "int"},
    "published_at": {"source": "published", "transform": "iso_datetime"}
  }
}

Respond with ONLY valid JSON in the format above. No markdown, no explanation."""

ANALYSIS_USER_TEMPLATE = """Analyze these {count} sample RSS entries and generate field mapping rules:

{samples}

Generate the field_mapping JSON that can extract structured fields from entries like these."""


# ---------------------------------------------------------------------------
# Non-streaming analysis
# ---------------------------------------------------------------------------

async def analyze_feed(entries: list[dict], sample_count: int = 5) -> dict:
    """Analyze sample RSS entries using LLM and generate field mappings.

    Supports both OpenRouter SDK (for reasoning model support) and
    OpenAI SDK (for other providers).

    Args:
        entries: List of feedparser entry dicts (as plain dicts).
        sample_count: Number of entries to send to LLM (default 5).

    Returns:
        Dict with keys: field_mapping, sample_results, confidence
    """
    samples = entries[:sample_count]
    empty_result = {"field_mapping": {}, "sample_results": [], "confidence": "low"}

    if not samples:
        logger.warning("No entries to analyze")
        return empty_result

    if not settings.llm_api_key:
        logger.warning("LLM API key not configured, cannot analyze feed")
        return empty_result

    user_content = ANALYSIS_USER_TEMPLATE.format(
        count=len(samples),
        samples=json.dumps(samples, ensure_ascii=False, indent=2, default=str),
    )
    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    # openrouter/free randomly selects a free model; some occasionally return
    # empty content, malformed JSON, or hit per-minute rate limits. Retry up to
    # 2 additional times, waiting 65 s on per-minute rate limit errors.
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            if _is_openrouter():
                content = await _call_openrouter(messages)
            else:
                content = await _call_openai(messages)

            if not content or not content.strip():
                logger.warning("LLM returned empty response (attempt %d/%d)", attempt, max_attempts)
                continue

            raw_mapping = _parse_llm_json(content)
            validated_mapping = _validate_mapping(raw_mapping)
            confidence = _calc_confidence(validated_mapping)

            return {
                "field_mapping": validated_mapping,
                "sample_results": [],
                "confidence": confidence,
            }

        except Exception as e:
            err = str(e).lower()
            if "per-day" in err or "per_day" in err:
                # Daily limit exhausted — no point retrying.
                logger.warning("LLM daily rate limit hit, skipping retries: %s", e)
                return empty_result
            if "rate limit" in err or "per-min" in err or "per_min" in err:
                if attempt < max_attempts:
                    logger.warning(
                        "LLM per-minute rate limit hit (attempt %d/%d), waiting 65s…",
                        attempt, max_attempts,
                    )
                    await asyncio.sleep(65)
                    continue
            logger.warning("LLM call failed (attempt %d/%d): %s", attempt, max_attempts, e)

    logger.error("LLM feed analysis: all %d attempts failed", max_attempts)
    return empty_result


async def _call_openrouter(messages: list[dict]) -> str:
    """Call OpenRouter using the native SDK.

    Uses streaming internally even for non-streaming requests because
    ``openrouter/free`` non-streaming mode sometimes returns classification
    text (e.g. ``'User Safety: safe'``) instead of the actual response.

    Compatible with both thinking (reasoning) and non-thinking models:

    - ``content`` and ``reasoning`` deltas are accumulated in **separate**
      buffers so a thinking model's trace never contaminates the JSON answer.
    - The final answer is taken from ``content``; ``reasoning`` is used only
      as a fallback when the model leaves ``content`` empty (some free
      reasoning models emit the answer only in the reasoning trace).
    - No ``reasoning`` parameter is sent. The ``openrouter/free`` router
      itself omits the ``reasoning`` field, and sending it would shrink the
      candidate pool; non-reasoning models reject the parameter outright.
    """
    from openrouter import OpenRouter

    async with OpenRouter(api_key=settings.llm_api_key) as client:
        res = await client.chat.send_async(
            messages=messages,
            model=settings.llm_model,
            stream=True,
        )

        content_buf = ""
        reasoning_buf = ""
        async for chunk in res:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            c = getattr(delta, "content", None) or ""
            r = (
                getattr(delta, "reasoning", None)
                or getattr(delta, "reasoning_content", None)
                or ""
            )
            if c:
                content_buf += c
            if r:
                reasoning_buf += r

        return content_buf if content_buf.strip() else reasoning_buf


async def _call_openai(messages: list[dict]) -> str:
    """Call an OpenAI-compatible API using the OpenAI SDK.

    ``enable_thinking`` is forwarded via ``extra_body`` so providers that
    support chain-of-thought (e.g. ZhipuAI GLM, DeepSeek-R1) respect it.
    Providers that don't recognise the field silently ignore it.
    """
    client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=httpx.Timeout(120.0, connect=10.0),
    )
    response = await client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        temperature=0.1,
        timeout=120,
        extra_body={"enable_thinking": settings.llm_enable_thinking},
    )
    msg = response.choices[0].message
    return _extract_content(msg)


async def call_llm(messages: list[dict]) -> str:
    """Call the configured LLM and return the text response.

    Public wrapper around the internal ``_call_openrouter`` / ``_call_openai``
    functions. Used by other services (e.g. ``metadata_agent``) that need
    LLM access without re-implementing the provider-detection logic.
    """
    if _is_openrouter():
        return await _call_openrouter(messages)
    else:
        return await _call_openai(messages)


# ---------------------------------------------------------------------------
# Streaming analysis
# ---------------------------------------------------------------------------

async def analyze_feed_stream(entries: list[dict], sample_count: int = 5) -> AsyncGenerator[dict, None]:
    """Stream LLM analysis as SSE-compatible dicts.

    Yields dicts with 'type' key:
    - {"type": "delta", "content": "partial text..."}  — text chunk
    - {"type": "done", "field_mapping": {...}, "confidence": "high"}  — final result
    - {"type": "error", "message": "error description"}  — error
    """
    samples = entries[:sample_count]

    if not samples:
        yield {"type": "error", "message": "No entries to analyze"}
        return

    if not settings.llm_api_key:
        yield {"type": "error", "message": "LLM API key not configured"}
        return

    user_content = ANALYSIS_USER_TEMPLATE.format(
        count=len(samples),
        samples=json.dumps(samples, ensure_ascii=False, indent=2, default=str),
    )
    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    try:
        stream_fn = _stream_openrouter if _is_openrouter() else _stream_openai
        async for event in stream_fn(messages):
            yield event
    except Exception as e:
        logger.error("Streaming LLM analysis failed: %s", e)
        yield {"type": "error", "message": str(e)}


async def _stream_openrouter(messages: list[dict]) -> AsyncGenerator[dict, None]:
    """Stream from OpenRouter using the native SDK.

    Compatible with both thinking and non-thinking models. Reasoning deltas
    (if any) typically arrive before content deltas; both are emitted as
    ``delta`` events so the client can render the full stream, but the final
    JSON is parsed from ``content`` when available, falling back to
    ``reasoning`` only if the model left ``content`` empty.

    Retries up to 3 times on transient failures (network errors, empty
    responses, JSON parse errors). Deltas are buffered until a successful
    parse so the client only sees a clean stream.
    """
    from openrouter import OpenRouter

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        content_buf = ""
        reasoning_buf = ""
        pending_deltas: list[dict] = []

        try:
            async with OpenRouter(api_key=settings.llm_api_key) as client:
                res = await client.chat.send_async(
                    messages=messages,
                    model=settings.llm_model,
                    stream=True,
                )
                async for chunk in res:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    c = getattr(delta, "content", None) or ""
                    r = (
                        getattr(delta, "reasoning", None)
                        or getattr(delta, "reasoning_content", None)
                        or ""
                    )
                    if c:
                        content_buf += c
                        pending_deltas.append({"type": "delta", "content": c})
                    elif r:
                        reasoning_buf += r
                        pending_deltas.append({"type": "delta", "content": r})
        except Exception as e:
            err = str(e).lower()
            if "per-day" in err or "per_day" in err:
                yield {"type": "error", "message": str(e)}
                return
            if attempt < max_attempts:
                if "rate limit" in err or "per-min" in err or "per_min" in err:
                    logger.warning(
                        "OpenRouter per-minute rate limit (attempt %d/%d), waiting 65s…",
                        attempt, max_attempts,
                    )
                    await asyncio.sleep(65)
                else:
                    logger.warning("OpenRouter stream attempt %d/%d failed: %s", attempt, max_attempts, e)
                continue
            yield {"type": "error", "message": str(e)}
            return

        final = content_buf if content_buf.strip() else reasoning_buf
        if not final.strip():
            if attempt < max_attempts:
                logger.warning("OpenRouter returned empty response (attempt %d/%d)", attempt, max_attempts)
                continue
            yield {"type": "error", "message": "LLM returned empty response"}
            return

        try:
            raw_mapping = _parse_llm_json(final)
        except json.JSONDecodeError:
            if attempt < max_attempts:
                logger.warning("JSON parse failed (attempt %d/%d)", attempt, max_attempts)
                continue
            yield {"type": "error", "message": "LLM returned invalid JSON"}
            return

        # Success — emit buffered deltas, then done
        for delta_event in pending_deltas:
            yield delta_event
        validated_mapping = _validate_mapping(raw_mapping)
        confidence = _calc_confidence(validated_mapping)
        yield {"type": "done", "field_mapping": validated_mapping, "confidence": confidence}
        return


async def _stream_openai(messages: list[dict]) -> AsyncGenerator[dict, None]:
    """Stream from an OpenAI-compatible API.

    Compatible with both thinking and non-thinking models. ``content`` and
    ``reasoning`` deltas are accumulated separately; the final JSON is parsed
    from ``content`` when available, falling back to ``reasoning`` otherwise.
    """
    client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=httpx.Timeout(120.0, connect=10.0),
    )

    stream = await client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        temperature=0.1,
        stream=True,
        timeout=120,
        extra_body={"enable_thinking": settings.llm_enable_thinking},
    )

    content_buf = ""
    reasoning_buf = ""
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        c = getattr(delta, "content", None) or ""
        r = (
            getattr(delta, "reasoning", None)
            or getattr(delta, "reasoning_content", None)
            or ""
        )
        if c:
            content_buf += c
            yield {"type": "delta", "content": c}
        elif r:
            reasoning_buf += r
            yield {"type": "delta", "content": r}

    final = content_buf if content_buf.strip() else reasoning_buf
    if not final.strip():
        yield {"type": "error", "message": "LLM returned empty response"}
        return

    try:
        raw_mapping = _parse_llm_json(final)
    except json.JSONDecodeError as e:
        yield {"type": "error", "message": f"Could not parse JSON from LLM response: {e}"}
        return
    validated_mapping = _validate_mapping(raw_mapping)
    confidence = _calc_confidence(validated_mapping)
    yield {"type": "done", "field_mapping": validated_mapping, "confidence": confidence}
