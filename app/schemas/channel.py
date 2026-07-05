"""Channel Pydantic schemas."""

import json as _json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from app.schemas.common import ORMModel


class ChannelCreate(BaseModel):
    name: str
    type: str = "rss_feed"
    url: str
    fetch_interval: int = 1800
    status: str = "active"
    field_mapping: dict
    metadata_agent_enabled: bool = True


class ChannelUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    fetch_interval: int | None = None
    status: str | None = None
    field_mapping: dict | None = None
    metadata_agent_enabled: bool | None = None


class ChannelResponse(ORMModel):
    id: str
    name: str
    type: str
    url: str
    fetch_interval: int
    status: str
    field_mapping: dict
    metadata_agent_enabled: bool = True
    last_fetched_at: datetime | None = None
    last_fetch_status: str | None = None
    last_fetch_error: str | None = None
    created_at: datetime
    updated_at: datetime


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
