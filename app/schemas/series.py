"""TVSeries Pydantic schemas."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class TVSeriesCreate(BaseModel):
    title_cn: str | None = None
    title_en: str | None = None
    aliases: list[str] | None = None
    external_id: str | None = None
    external_source: str | None = None
    description: str | None = None
    genre: list[str] | None = None
    start_date: date | None = None
    content_type: str | None = None


class TVSeriesUpdate(BaseModel):
    title_cn: str | None = None
    title_en: str | None = None
    aliases: list[str] | None = None
    external_id: str | None = None
    external_source: str | None = None
    description: str | None = None
    genre: list[str] | None = None
    start_date: date | None = None
    content_type: str | None = None


class TVSeriesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title_cn: str | None = None
    title_en: str | None = None
    aliases: list[str] | None = None
    external_id: str | None = None
    external_source: str | None = None
    description: str | None = None
    genre: list[str] | None = None
    start_date: date | None = None
    content_type: str | None = None
    created_at: datetime
    updated_at: datetime
