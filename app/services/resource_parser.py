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
