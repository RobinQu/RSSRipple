"""Pydantic schemas for API-key management."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator


def _require_future_expiry(v: datetime | None) -> datetime | None:
    """expires_at must be a tz-aware ISO 8601 timestamp in the future."""
    if v is None:
        return v
    if v.tzinfo is None:
        raise ValueError("expires_at must include a timezone (ISO 8601 UTC)")
    if v <= datetime.now(UTC):
        raise ValueError("expires_at must be in the future")
    return v


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # Optional expiry (ISO 8601 UTC, must be future); null/omitted = never.
    expires_at: datetime | None = None

    _check_expires_at = field_validator("expires_at")(_require_future_expiry)


class ApiKeyRotate(BaseModel):
    """Optional rotate body: a new expiry; omitted/null inherits the old key's."""

    expires_at: datetime | None = None

    _check_expires_at = field_validator("expires_at")(_require_future_expiry)


class ApiKeyResponse(BaseModel):
    """Public view of an API key — never includes the hash or plaintext."""

    id: str
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime | None


class ApiKeyCreatedResponse(ApiKeyResponse):
    """Create/rotate response — the ONLY place the plaintext key is returned."""

    key: str
