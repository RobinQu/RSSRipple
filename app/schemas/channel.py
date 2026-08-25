"""Channel Pydantic schemas."""

import json as _json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.common import ORMModel


def _default_required_fields() -> list[str]:
    """Baseline required-fields list for new channels (code-enforced tier)."""
    from app.services.required_fields import normalize_required_fields

    return normalize_required_fields([])


class ChannelCreate(BaseModel):
    name: str
    type: str = "rss_feed"
    url: str
    fetch_interval: int = 1800
    status: str = "active"
    field_mapping: dict
    metadata_agent_enabled: bool = True
    metadata_source: str | None = None
    metadata_fallback_sources: list[str] | None = None
    # Mandatory and add-only after creation (see channels API): defaults to
    # the code-enforced baseline; explicit null is rejected by the type.
    required_metadata_fields: list[str] = Field(default_factory=_default_required_fields)
    auto_cleanup_unresolved_enabled: bool = False
    auto_cleanup_unresolved_days: int = 21
    # Periodic work-metadata refresh (per-channel; off by default).
    metadata_refresh_enabled: bool = False
    metadata_refresh_interval_minutes: int | None = None
    metadata_refresh_full_scope: bool = False
    # "默认标记为 Anime" — immutable after creation (see channels API).
    default_is_anime: bool = False

    @field_validator("metadata_source")
    @classmethod
    def _validate_source(cls, v: str | None) -> str | None:
        return _normalize_source(v)

    @field_validator("metadata_fallback_sources")
    @classmethod
    def _validate_fallback_sources(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_fallback_sources(v)

    @field_validator("required_metadata_fields")
    @classmethod
    def _validate_required_fields(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_required_fields(v)

    @field_validator("auto_cleanup_unresolved_days")
    @classmethod
    def _clamp_days(cls, v: int) -> int:
        return _clamp_cleanup_days(v)

    @field_validator("metadata_refresh_interval_minutes")
    @classmethod
    def _clamp_refresh_interval(cls, v: int | None) -> int | None:
        return None if v is None else _clamp_refresh_interval_minutes(v)


class ChannelUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    fetch_interval: int | None = None
    field_mapping: dict | None = None
    metadata_agent_enabled: bool | None = None
    metadata_source: str | None = None
    metadata_fallback_sources: list[str] | None = None
    required_metadata_fields: list[str] | None = None
    auto_cleanup_unresolved_enabled: bool | None = None
    auto_cleanup_unresolved_days: int | None = None
    # Periodic work-metadata refresh (per-channel; off by default).
    metadata_refresh_enabled: bool | None = None
    metadata_refresh_interval_minutes: int | None = None
    metadata_refresh_full_scope: bool | None = None
    # Immutable after creation — the update endpoint 422s when the submitted
    # value differs from the stored one.
    default_is_anime: bool | None = None

    @field_validator("metadata_source")
    @classmethod
    def _validate_source(cls, v: str | None) -> str | None:
        return _normalize_source(v)

    @field_validator("metadata_fallback_sources")
    @classmethod
    def _validate_fallback_sources(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_fallback_sources(v)

    @field_validator("required_metadata_fields")
    @classmethod
    def _validate_required_fields(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_required_fields(v)

    @field_validator("auto_cleanup_unresolved_days")
    @classmethod
    def _clamp_days(cls, v: int | None) -> int | None:
        return None if v is None else _clamp_cleanup_days(v)

    @field_validator("metadata_refresh_interval_minutes")
    @classmethod
    def _clamp_refresh_interval(cls, v: int | None) -> int | None:
        return None if v is None else _clamp_refresh_interval_minutes(v)


class ChannelResponse(ORMModel):
    id: str
    name: str
    type: str
    url: str
    fetch_interval: int
    status: str
    field_mapping: dict
    metadata_agent_enabled: bool = True
    metadata_source: str | None = None
    metadata_fallback_sources: list[str] | None = None
    required_metadata_fields: list[str] | None = None
    auto_cleanup_unresolved_enabled: bool = False
    auto_cleanup_unresolved_days: int = 21
    metadata_refresh_enabled: bool = False
    metadata_refresh_interval_minutes: int | None = None
    metadata_refresh_full_scope: bool = False
    default_is_anime: bool = False
    last_fetched_at: datetime | None = None
    last_fetch_status: str | None = None
    last_fetch_error: str | None = None
    created_at: datetime
    updated_at: datetime


def _clamp_cleanup_days(v: int) -> int:
    """Clamp the auto-cleanup threshold to a sane positive window."""
    if v < 1:
        return 1
    if v > 365:
        return 365
    return v


def _clamp_refresh_interval_minutes(v: int | None) -> int | None:
    """Clamp the periodic works-refresh interval to the supported window."""
    if v is None:
        return None
    from app.services.settings_service import (
        MAX_METADATA_REFRESH_INTERVAL_MINUTES,
        MIN_METADATA_REFRESH_INTERVAL_MINUTES,
    )

    return max(MIN_METADATA_REFRESH_INTERVAL_MINUTES, min(v, MAX_METADATA_REFRESH_INTERVAL_MINUTES))


def _normalize_source(value: str | None) -> str | None:
    """Lowercase + validate a channel metadata source. Empty/None passes through.

    Channel config is restricted to the two-source architecture
    (wikipedia/tmdb); legacy exa/jina/local/combined values are rejected.
    """
    from app.services.metadata_sources import SUPPORTED_CHANNEL_METADATA_SOURCES

    if value is None:
        return None
    v = value.strip().lower()
    if not v:
        return None
    if v not in SUPPORTED_CHANNEL_METADATA_SOURCES:
        raise ValueError(f"unsupported metadata_source: {value!r}")
    return v


def _normalize_fallback_sources(value: list[str] | None) -> list[str] | None:
    """Validate the ordered Exa-fallback whitelist against the site registry.

    None = use the default order; [] = fallback disabled. Unknown site names
    are rejected.
    """
    from app.services.metadata_source_registry import REGISTRY_SOURCES

    if value is None:
        return None
    out: list[str] = []
    for item in value:
        v = str(item).strip().lower()
        if v not in REGISTRY_SOURCES:
            raise ValueError(f"unsupported metadata fallback source: {item!r}")
        if v not in out:
            out.append(v)
    return out


def _normalize_required_fields(value: list[str] | None) -> list[str] | None:
    """Validate the channel's required work-metadata fields against the
    catalog.

    The list is mandatory and add-only after creation: explicit ``None`` is
    rejected (there is no "unrestricted" state — the code-enforced baseline is
    always required), unknown keys are rejected, duplicates drop, the locked
    baseline is force-included, and the result is reordered into canonical
    catalog order.
    """
    from app.services.required_fields import (
        REQUIRED_FIELD_CATALOG,
        normalize_required_fields,
        validate_required_fields,
    )

    if value is None:
        raise ValueError(
            "required_metadata_fields cannot be cleared: the code-enforced "
            f"baseline ({', '.join(sorted(normalize_required_fields([])))}) is always required"
        )
    errs = validate_required_fields(value)
    if errs:
        raise ValueError(
            f"unsupported required_metadata_fields (catalog: {', '.join(REQUIRED_FIELD_CATALOG)}): "
            + "; ".join(errs)
        )
    return normalize_required_fields(value)


class ChannelListItem(ChannelResponse):
    agent_count: int = 0
    resource_count: int = 0


class ValidateURLRequest(BaseModel):
    url: str


class PreviewFeedRequest(BaseModel):
    url: str
    field_mapping: dict | None = None


class SummarizeFiltersRequest(BaseModel):
    resource_ids: list[str]

    @model_validator(mode="before")
    @classmethod
    def _decode_bytes(cls, v: Any) -> Any:
        if isinstance(v, (bytes, bytearray)):
            return _json.loads(v)
        return v


class FilterSuggestion(BaseModel):
    field: str
    operator: str
    value: Any
    confidence: float
    label: str


class FetchStatusResponse(BaseModel):
    status: str | None = None
    result: Any = None
    error: str | None = None
