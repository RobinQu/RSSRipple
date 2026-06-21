"""Channel Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ChannelCreate(BaseModel):
    name: str
    type: str = "rss_feed"
    url: str
    fetch_interval: int = 1800


class ChannelUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    fetch_interval: int | None = None
    status: str | None = None


class ChannelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type: str
    url: str
    fetch_interval: int
    status: str
    last_fetched_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ValidateURLRequest(BaseModel):
    url: str


class ValidateURLResponse(BaseModel):
    valid: bool
    message: str
    item_count: int = 0
