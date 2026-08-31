"""Canonical parsing and compatibility helpers for release subtitle groups."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select

# Do not treat slash separators in bilingual titles as publisher separators.
# A double plus is commonly part of a literal release name, so only a lone
# plus is accepted as a joiner.
_JOINER_RE = re.compile(r"(?<!\+)\+(?!\+)|[&＆＋×]")
_HTML_RE = re.compile(r"&(?:amp|#38|#x26);", re.IGNORECASE)


def decode_group_text(value: Any) -> str:
    """Decode feed HTML entities repeatedly, then trim surrounding space."""
    text = "" if value is None else str(value)
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text.strip()


def normalize_group_key(value: Any) -> str:
    return decode_group_text(value).casefold()


def split_subtitle_group_candidates(value: Any) -> list[str]:
    """Split explicit publisher joiners while preserving order and casing."""
    text = decode_group_text(value)
    if not text:
        return []
    parts = _JOINER_RE.split(text)
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        item = part.strip()
        key = normalize_group_key(item)
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def normalize_subtitle_groups(value: Any, *, split: bool = True) -> list[str]:
    """Return a stable, case-insensitive-deduplicated group list.

    ``value`` may be the legacy scalar, a list returned by the new LLM schema,
    or ``None``.  ``split=False`` is used for unresolved compound values so the
    original release label remains lossless.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        source: Iterable[Any] = value
    else:
        source = split_subtitle_group_candidates(value) if split else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in source:
        text = decode_group_text(item)
        key = normalize_group_key(text)
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def join_legacy_subtitle_group(value: Any) -> str | None:
    groups = normalize_subtitle_groups(value)
    return "&".join(groups) if groups else None


def subtitle_groups_for_resource(resource: Any) -> list[str]:
    """Read the canonical list, falling back to the legacy scalar column."""
    groups = normalize_subtitle_groups(getattr(resource, "subtitle_groups", None), split=False)
    if groups:
        return groups
    return normalize_subtitle_groups(getattr(resource, "subtitle_group", None))


def has_group_separator(value: Any) -> bool:
    text = decode_group_text(value)
    return bool(_JOINER_RE.search(text))


def canonical_group_set(value: Any) -> frozenset[str]:
    return frozenset(normalize_group_key(x) for x in normalize_subtitle_groups(value))


async def resolve_subtitle_groups(db: Any, value: Any) -> tuple[list[str], str]:
    """Resolve a scalar label using the learned mapping table.

    A compound value is split only when every candidate is already known as a
    standalone/confirmed group.  Otherwise the lossless decoded scalar is
    retained and marked unresolved for the metadata judge.
    """
    from app.models.subtitle_group_mapping import SubtitleGroupMapping

    raw = decode_group_text(value)
    if not raw:
        return [], "single"
    key = normalize_group_key(raw)
    mapping = (await db.execute(
        select(SubtitleGroupMapping).where(SubtitleGroupMapping.normalized_key == key)
    )).scalar_one_or_none()
    if mapping is not None and mapping.resolution in {"llm", "manual"}:
        return normalize_subtitle_groups(mapping.groups, split=False), mapping.resolution
    candidates = split_subtitle_group_candidates(raw)
    if len(candidates) <= 1:
        return candidates, "single"
    known_rows = (await db.execute(
        select(SubtitleGroupMapping.groups).where(
            SubtitleGroupMapping.resolution.in_(("single", "llm", "manual"))
        )
    )).scalars().all()
    known = {normalize_group_key(member) for groups in known_rows for member in (groups or [])}
    if all(normalize_group_key(candidate) in known for candidate in candidates):
        return candidates, "heuristic"
    return [raw], "unresolved"


__all__ = [
    "canonical_group_set",
    "decode_group_text",
    "has_group_separator",
    "join_legacy_subtitle_group",
    "normalize_group_key",
    "normalize_subtitle_groups",
    "split_subtitle_group_candidates",
    "subtitle_groups_for_resource",
    "resolve_subtitle_groups",
]
