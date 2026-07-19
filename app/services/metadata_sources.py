"""Metadata source catalog & configuration helpers.

Pure leaf module - no DB, no LLM. Extracted verbatim from metadata_agent.py
(Phase 0 leaf extraction): the set of selectable external metadata sources,
their enable/configured flags, and source-type normalization.
"""
from __future__ import annotations

from typing import Any

from app.services.runtime_config import runtime_config

DEFAULT_METADATA_SOURCE = "exa"
SUPPORTED_METADATA_SOURCES = {"tmdb", "exa", "wikipedia", "jina", "local"}

# User-selectable external metadata sources (ordered as presented in the UI).
# ``key`` is the credential attr on Settings; sources without a key
# (wikipedia) are considered configured whenever their enable switch is on.
_EXTERNAL_SOURCE_DEFS: tuple[dict[str, str], ...] = (
    {"value": "exa", "label": "Exa Agent", "key": "exa_api_key",
     "description": "Structured web-agent search; broad evidence coverage."},
    {"value": "jina", "label": "Jina Search + Reader", "key": "jina_api_key",
     "description": "Cheap web-native search with strong CJK coverage."},
    {"value": "wikipedia", "label": "Wikipedia", "key": "",
     "description": "Wikipedia REST search; no API key required."},
    {"value": "tmdb", "label": "TMDB", "key": "tmdb_api_key",
     "description": "The Movie Database; best for TV/movie ID matching."},
)


def is_metadata_source_configured(source: str) -> bool:
    """Whether the credentials for *source* are present (key set)."""
    for d in _EXTERNAL_SOURCE_DEFS:
        if d["value"] == source:
            return True if not d["key"] else bool(getattr(runtime_config, d["key"], ""))
    return False


def is_metadata_source_enabled(source: str) -> bool:
    """Whether the enable switch for *source* is on."""
    flag = {
        "exa": runtime_config.exa_enabled,
        "jina": runtime_config.jina_enabled,
        "tmdb": runtime_config.tmdb_enabled,
        "wikipedia": runtime_config.wikipedia_enabled,
    }.get(source)
    return bool(flag)


def is_metadata_source_available(source: str) -> bool:
    """A source is an selectable candidate when enabled AND configured."""
    return is_metadata_source_enabled(source) and is_metadata_source_configured(source)


def get_metadata_source_catalog() -> list[dict[str, Any]]:
    """Return all external metadata sources with their availability flags.

    Each entry: ``{value, label, description, enabled, configured, available}``.
    The frontend offers only ``available`` sources in the channel form.
    """
    catalog: list[dict[str, Any]] = []
    for d in _EXTERNAL_SOURCE_DEFS:
        value = d["value"]
        catalog.append({
            "value": value,
            "label": d["label"],
            "description": d["description"],
            "enabled": is_metadata_source_enabled(value),
            "configured": is_metadata_source_configured(value),
            "available": is_metadata_source_available(value),
        })
    return catalog


def get_available_metadata_sources() -> list[dict[str, Any]]:
    """Return only the currently-selectable external metadata sources."""
    return [s for s in get_metadata_source_catalog() if s["available"]]


def resolve_metadata_source(value: str | None) -> str:
    """Resolve a channel's stored source to a runnable source.

    Returns the normalized source if it is supported, else the default. Callers
    that need an *available* source should additionally check
    :func:`is_metadata_source_available` and fall back.
    """
    return normalize_metadata_source_type(value)


def normalize_metadata_source_type(value: str | None) -> str:
    """Normalize a caller-provided metadata source.

    ``combined`` is accepted only as a legacy dataset value and maps to the
    default single source. ``local`` searches the in-app TVSeries/Movie library
    via FTS5 instead of calling an external API. New calls should pass
    tmdb/exa/wikipedia/local explicitly.
    """
    source = (value or DEFAULT_METADATA_SOURCE).strip().lower()
    if source == "combined":
        return DEFAULT_METADATA_SOURCE
    return source if source in SUPPORTED_METADATA_SOURCES else DEFAULT_METADATA_SOURCE
