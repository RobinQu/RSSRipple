"""Dynamic resource parser using per-channel field mappings.

Uses the new field_mapping format with list_locator + field_mappings.
Backward compatible with the old flat dict format.
"""

import re
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def parse_entry(entry: dict, field_mapping: dict | None, description: str | None = None) -> dict:
    """Parse a feedparser entry into FileResource fields.

    If field_mapping is None or empty, returns an empty dict.
    Supports two formats:
    - New format: {"list_locator": {...}, "field_mappings": {...}}
    - Old flat format: {"field_name": {"source": "...", ...}, ...}

    Args:
        entry: A feedparser entry as a plain dict.
        field_mapping: Channel-specific field mapping rules.
        description: Optional entry description (unused, kept for API compatibility).

    Returns:
        Dict of parsed FileResource fields.
    """
    if not field_mapping:
        return {}

    # New format: extract field_mappings from the wrapper
    if "field_mappings" in field_mapping:
        mappings = field_mapping["field_mappings"]
    else:
        # Backward compat: treat the whole dict as field_mappings
        mappings = field_mapping

    return _parse_with_mappings(entry, mappings)


def _parse_with_mappings(entry: dict, field_mappings: dict) -> dict:
    """Parse a feedparser entry dict using per-field extraction rules.

    Args:
        entry: A feedparser entry as a plain dict.
        field_mappings: Dict mapping FileResource field names to extraction rules.
            Each rule is a dict with keys: source, regex (optional),
            group (optional), transform (optional).

    Returns:
        Dict of parsed FileResource fields.
    """
    result = {}
    for field_name, rule in field_mappings.items():
        try:
            value = _extract_value(entry, rule)
            result[field_name] = value
        except Exception as e:
            logger.debug("Failed to extract field '%s': %s", field_name, e)
            result[field_name] = None
    return result


def _extract_value(entry: dict, rule: dict) -> Any:
    """Extract a single value from an entry using a mapping rule."""
    source = rule.get("source", "")
    raw_value = _resolve_source(entry, source)

    if raw_value is None:
        return None

    raw_str = str(raw_value)

    # Apply regex extraction if specified
    regex = rule.get("regex")
    if regex:
        group = rule.get("group", 0)
        match = re.search(regex, raw_str)
        if match:
            raw_str = match.group(group)
        else:
            return None

    # Apply transform if specified
    transform = rule.get("transform")
    return _apply_transform(raw_str, transform)


def _resolve_source(entry: dict, source_path: str) -> Any:
    """Resolve a dotted/indexed path against a feedparser entry dict.

    Supports paths like: "title", "enclosures[0].url", "description"
    """
    if not source_path:
        return None

    parts = source_path.split(".")
    current = entry

    for part in parts:
        if current is None:
            return None

        # Handle array indexing: "enclosures[0]"
        bracket_match = re.match(r"^(.+?)\[(\d+)\]$", part)
        if bracket_match:
            key = bracket_match.group(1)
            index = int(bracket_match.group(2))
            if isinstance(current, dict):
                arr = current.get(key)
            else:
                arr = getattr(current, key, None)
            if arr and index < len(arr):
                current = arr[index]
            else:
                return None
        else:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = getattr(current, part, None)

    return current


def _apply_transform(value: str, transform: str | None) -> Any:
    """Apply a type transformation to a string value."""
    if transform is None:
        return value

    if transform == "int":
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    elif transform == "float":
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    elif transform == "iso_datetime":
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    elif transform == "lowercase":
        return value.lower()
    elif transform == "uppercase":
        return value.upper()

    return value


# ---------------------------------------------------------------------------
# Multi-episode batch (合集) detection
# ---------------------------------------------------------------------------

# Ordered pattern list: earlier patterns are tried first. Each pattern returns
# ``(is_batch, start, end)`` — start/end may be None when the title marks a
# batch (Season Pack / 全集 / Fin) without explicit boundaries.

_BATCH_PATTERNS: list[tuple[re.Pattern[str], int, int]] = [
    # SxxEyy~zz  /  SxxEyy-zz  /  SxxEyy–zz  (with optional Exx suffix on rhs)
    (re.compile(r"S\d+\s*E(\d{1,3})\s*[~\-–]\s*E?(\d{1,3})", re.IGNORECASE), 1, 2),
    # [01-12 合集] / [01~12 Fin] / [01-12] with batch keyword nearby
    (re.compile(
        r"\[\s*(\d{1,3})\s*[~\-–]\s*(\d{1,3})\s*(?:合集|Batch|Fin|完结|全集|完整|Complete)?\s*\]",
        re.IGNORECASE,
    ), 1, 2),
    # 01-12 合集 (no bracket)
    (re.compile(
        r"(\d{1,3})\s*[~\-–]\s*(\d{1,3})\s*(?:合集|Batch|Fin|完结|全集|完整|Complete)",
        re.IGNORECASE,
    ), 1, 2),
    # 第01-第12话 / 第01~12話
    (re.compile(r"第\s*(\d{1,3})\s*[~\-–]\s*第?\s*(\d{1,3})\s*[话話集]"), 1, 2),
]

_BATCH_KEYWORD_RE = re.compile(
    r"(?:Season\s*Pack|Full\s*Season|Batch|BD-?BOX|BDBOX|BD\s*Rip\s*Box|"
    r"全集|全季|合集|完整|完结|Complete\s*Series)",
    re.IGNORECASE,
)


def detect_batch(title: str | None) -> tuple[bool, int | None, int | None]:
    """Heuristically detect whether a raw RSS title represents a multi-episode
    batch (合集) resource.

    Returns ``(is_batch, episode_start, episode_end)``. When the title marks a
    batch without explicit boundaries (e.g. "Season Pack", "全集"), the two
    integers are None but ``is_batch`` is True.

    The MetadataAgent LLM may later refine or overwrite these values; the
    pre-parser exists so downstream logic stays safe even when the LLM path
    fails or is disabled.
    """
    if not title:
        return False, None, None

    for pattern, gstart, gend in _BATCH_PATTERNS:
        m = pattern.search(title)
        if not m:
            continue
        try:
            start = int(m.group(gstart))
            end = int(m.group(gend))
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        # Filter obvious false positives: ranges that look like resolution
        # tokens (e.g. "1920x1080") or single-year matches would already be
        # excluded by the leading anchors, but keep a sanity cap.
        if end - start > 200 or start < 0 or end > 999:
            continue
        return True, start, end

    if _BATCH_KEYWORD_RE.search(title):
        return True, None, None

    return False, None, None


# ---------------------------------------------------------------------------
# Cross-season episode reconciliation — pre-parser
# ---------------------------------------------------------------------------

# ``NN(MM)`` — per-season NN with an absolute MM in parens. Common on Chinese
# fansub packs, e.g. "13(85)" means "S4 episode 13, cumulative episode 85".
# The regex only matches when the inner number is ≥ the outer one, which is
# the shape that makes sense (absolute count is ≥ per-season count).
_EPISODE_NN_MM_RE = re.compile(
    r"(?<!\d)"           # not part of a larger number
    r"(\d{1,3})"          # per-season NN
    r"\s*\(\s*"
    r"(\d{2,4})"          # absolute MM (usually 2+ digits so we don't match
                          # runtimes like (24) accidentally — see filter below)
    r"\s*\)"
)


def detect_absolute_episode(title: str | None) -> tuple[int | None, int | None]:
    """Best-effort ``NN(MM)`` extraction from ``title``.

    Returns ``(per_season_ep, absolute_ep)``. Both are ``None`` when the
    title doesn't use the double-labeled form. When we do get a hit we
    require ``absolute > per_season`` and ``absolute - per_season ≥ 10`` —
    otherwise the parenthesized number is very likely a runtime, part
    number, or resolution decorator rather than an absolute episode count.
    """
    if not title:
        return None, None
    for m in _EPISODE_NN_MM_RE.finditer(title):
        try:
            per_season = int(m.group(1))
            absolute = int(m.group(2))
        except (TypeError, ValueError):
            continue
        # Sanity check — a real "13(85)" jump implies at least ~10 episodes
        # of earlier seasons. Small gaps (13(15)) are almost always something
        # else (e.g. resolution "1080p (15GB)" shreds).
        if absolute > per_season and absolute - per_season >= 10:
            return per_season, absolute
    return None, None


# ---------------------------------------------------------------------------
# Subtitle language detection
# ---------------------------------------------------------------------------

# Ordered from most-specific to least-specific so combos like "简繁日" hit
# before "简繁", and both hit before "简" / "繁" on their own.
_SUBTITLE_LANG_PATTERNS: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(r"简繁日", re.IGNORECASE), ["zh-CN", "zh-TW", "ja"]),
    (re.compile(r"简繁英", re.IGNORECASE), ["zh-CN", "zh-TW", "en"]),
    (re.compile(r"简繁", re.IGNORECASE), ["zh-CN", "zh-TW"]),
    (re.compile(r"(?:多国字幕|多语言|多語言|Multi[-_ ]?Sub)", re.IGNORECASE), ["multi"]),
    (re.compile(r"(?:\bCHS\b|简中|简体|GB(?![A-Z]))"), ["zh-CN"]),
    (re.compile(r"(?:\bCHT\b|繁中|繁体|繁體|BIG5)"), ["zh-TW"]),
    (re.compile(r"(?:\bJPN?\b|\bJAP\b|日语|日文|Japanese)", re.IGNORECASE), ["ja"]),
    (re.compile(r"(?:\bENG?\b|英字|英文|English)", re.IGNORECASE), ["en"]),
]


def detect_subtitle_langs(title: str | None) -> list[str]:
    """Return a de-duplicated list of BCP-47 language tags found in ``title``.

    An empty list means "parsed but no subtitle-language marker present". The
    caller decides whether to store ``None`` (never parsed) versus ``[]``
    (parsed, none found).

    Tags are appended in the order patterns match, preserving intent — e.g.
    ``"[CHS][CHT][ENG]"`` returns ``["zh-CN", "zh-TW", "en"]``.

    The sentinel tag ``"multi"`` is returned only when the title uses
    "multi-language" style shorthand without spelling out which languages.
    """
    if not title:
        return []
    seen: list[str] = []
    remaining = title
    for pattern, tags in _SUBTITLE_LANG_PATTERNS:
        if not pattern.search(remaining):
            continue
        for tag in tags:
            if tag not in seen:
                seen.append(tag)
        # Blank out matches so more general patterns don't re-fire on the same
        # substring (e.g. don't hit "CHS" inside a "简繁" span we already
        # translated to zh-CN + zh-TW).
        remaining = pattern.sub(" ", remaining)
    return seen
