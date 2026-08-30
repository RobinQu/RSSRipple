"""Metadata source catalog & configuration helpers.

Pure leaf module - no DB, no LLM. Extracted verbatim from metadata_agent.py
(Phase 0 leaf extraction): the set of selectable external metadata sources,
their enable/configured flags, and source-type normalization.
"""
from __future__ import annotations

from typing import Any

from app.services.runtime_config import runtime_config

DEFAULT_METADATA_SOURCE = "wikipedia"
# The same three primary sources are used by channels, manual search, refresh,
# batch jobs, and evaluation.
SUPPORTED_METADATA_SOURCES = {"tmdb", "wikipedia", "bangumi"}
# Sources selectable as a channel's primary metadata source.
SUPPORTED_CHANNEL_METADATA_SOURCES = {"wikipedia", "tmdb", "bangumi"}

# User-selectable external metadata sources (ordered as presented in the UI).
# ``key`` is the credential attr on Settings; sources without a key
# (wikipedia) are considered configured whenever their enable switch is on.
_EXTERNAL_SOURCE_DEFS: tuple[dict[str, str], ...] = (
    {"value": "wikipedia", "label": "Wikipedia", "key": "",
     "description": "Wikipedia REST search; no API key required."},
    {"value": "tmdb", "label": "TMDB", "key": "tmdb_api_key",
     "description": "The Movie Database; best for TV/movie ID matching."},
    {"value": "bangumi", "label": "Bangumi", "key": "bangumi_api_key",
     "description": "Bangumi subject search; anime-only category, "
                    "matched works are marked is_anime."},
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
        "tmdb": runtime_config.tmdb_enabled,
        "wikipedia": runtime_config.wikipedia_enabled,
        # No separate switch by design: a configured token IS the enablement.
        "bangumi": True,
    }.get(source)
    return bool(flag)


def is_metadata_source_available(source: str) -> bool:
    """A source is an selectable candidate when enabled AND configured."""
    return is_metadata_source_enabled(source) and is_metadata_source_configured(source)


def get_metadata_source_catalog(channel_only: bool = False) -> list[dict[str, Any]]:
    """Return external metadata sources with their availability flags.

    Each entry: ``{value, label, description, enabled, configured, available}``.
    With ``channel_only=True`` the result is restricted to the channel
    architecture (wikipedia/tmdb/bangumi); the full catalog is still used by
    the works-page refresh config, where the legacy manual sources remain
    selectable.
    """
    catalog: list[dict[str, Any]] = []
    for d in _EXTERNAL_SOURCE_DEFS:
        value = d["value"]
        if channel_only and value not in SUPPORTED_CHANNEL_METADATA_SOURCES:
            continue
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

    Channel resolution is restricted to the channel architecture
    (wikipedia/tmdb/bangumi): deprecated channel sources
    historical values, None, and unknown values all resolve to the
    default. Callers that need an *available* source should additionally check
    :func:`is_metadata_source_available` and fall back.
    """
    return normalize_channel_metadata_source(value)


def normalize_channel_metadata_source(value: str | None) -> str:
    """Normalize a *channel* metadata source to wikipedia/tmdb/bangumi.

    Deprecated channel sources, None, and unknown
    values map to the default (wikipedia). Used only for channel resolution;
    manual search / eval paths that still exercise the legacy ReAct sources
    go through :func:`normalize_metadata_source_type` instead.
    """
    source = (value or "").strip().lower()
    return source if source in SUPPORTED_CHANNEL_METADATA_SOURCES else DEFAULT_METADATA_SOURCE


def normalize_metadata_source_type(value: str | None) -> str:
    """Normalize a caller-provided metadata source.

    Only the three canonical primary sources are runnable. Historical source
    labels are deliberately normalised to Wikipedia and are never exposed by
    the source catalog.
    """
    source = (value or DEFAULT_METADATA_SOURCE).strip().lower()
    return source if source in SUPPORTED_METADATA_SOURCES else DEFAULT_METADATA_SOURCE
