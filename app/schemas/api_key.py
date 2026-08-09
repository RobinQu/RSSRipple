"""Pydantic schemas for API-key management."""

from datetime import datetime

from pydantic import BaseModel, Field


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class ApiKeyResponse(BaseModel):
    """Public view of an API key — never includes the hash or plaintext."""

    id: str
    name: str
    prefix: str
    created_at: datetime


class ApiKeyCreatedResponse(ApiKeyResponse):
    """Create response — the ONLY place the plaintext key is returned."""

    key: str
