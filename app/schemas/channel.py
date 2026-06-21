"""Channel Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ChannelCreate(BaseModel):
    name: str
    type: str = "rss_feed"
    url: str
    fetch_interval: int = 1800
    field_mapping: dict | None = None
    parser_type: str = "auto"


class ChannelUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    fetch_interval: int | None = None
    status: str | None = None
    field_mapping: dict | None = None
    parser_type: str | None = None


class ChannelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type: str
    url: str
    fetch_interval: int
    status: str
    field_mapping: dict | None = None
    parser_type: str
    last_fetched_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ValidateURLRequest(BaseModel):
    url: str


class ValidateURLResponse(BaseModel):
    valid: bool
    message: str
    item_count: int = 0
    downloadable_count: int = 0


class FieldMappingEntry(BaseModel):
    """A single field extraction rule for dynamic RSS parsing."""
    source: str  # feedparser entry field path, e.g. "title", "enclosures[0].url"
    regex: str | None = None  # optional regex to extract from source
    group: int = 0  # regex capture group index (0 = full match)
    transform: str | None = None  # type coercion: "int", "float", "iso_datetime", "lowercase", "uppercase"


class FeedAnalysisResponse(BaseModel):
    """Response from LLM-based RSS feed analysis."""
    field_mapping: dict[str, FieldMappingEntry]
    sample_results: list[dict]  # parsed results for sample entries (for user review)
    confidence: str  # "high", "medium", "low"
