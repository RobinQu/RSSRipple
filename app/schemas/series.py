"""TVSeries Pydantic schemas."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class TVSeriesCreate(BaseModel):
    title_cn: str | None = None
    title_en: str | None = None
    original_title: str | None = None
    aliases: list[str] | None = None
    external_id: str | None = None
    external_source: str | None = None
    description: str | None = None
    poster_url: str | None = None
    rating: float | None = None
    genre: list[str] | None = None
    status: str | None = None
    number_of_episodes: int | None = None
    number_of_seasons: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    content_type: str | None = "tv"


class TVSeriesUpdate(BaseModel):
    title_cn: str | None = None
    title_en: str | None = None
    original_title: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    poster_url: str | None = None
    rating: float | None = None
    genre: list[str] | None = None
    status: str | None = None
    number_of_episodes: int | None = None
    number_of_seasons: int | None = None
    start_date: date | None = None
    end_date: date | None = None


class TVSeriesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title_cn: str | None = None
    title_en: str | None = None
    original_title: str | None = None
    aliases: list[str] | None = None
    external_id: str | None = None
    external_source: str | None = None
    description: str | None = None
    poster_url: str | None = None
    rating: float | None = None
    genre: list[str] | None = None
    status: str | None = None
    number_of_episodes: int | None = None
    number_of_seasons: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    content_type: str | None = None
    created_at: datetime
    updated_at: datetime
