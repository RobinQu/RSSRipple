"""TVSeries Pydantic schemas."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.genre import GenreName


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
    genre: list[GenreName] | None = None
    status: str | None = None
    number_of_episodes: int | None = None
    number_of_seasons: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    content_type: str | None = "tv"
    is_anime: bool | None = None


class TVSeriesUpdate(BaseModel):
    title_cn: str | None = None
    title_en: str | None = None
    original_title: str | None = None
    aliases: list[str] | None = None
    external_id: str | None = None
    external_source: str | None = None
    description: str | None = None
    poster_url: str | None = None
    rating: float | None = None
    genre: list[GenreName] | None = None
    status: str | None = None
    number_of_episodes: int | None = None
    number_of_seasons: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    content_type: Literal["tv", "movie"] | None = None
    is_anime: bool | None = None


class TVSeriesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title_cn: str | None = None
    title_en: str | None = None
    original_title: str | None = None
    aliases: list[str] | None = None
    external_id: str | None = None
    external_source: str | None = None
    canonical_name: str | None = None
    wikipedia_url: str | None = None
    description: str | None = None
    poster_url: str | None = None
    rating: float | None = None
    genre: list[GenreName] | None = None
    status: str | None = None
    number_of_episodes: int | None = None
    number_of_seasons: int | None = None
    # Per-season works: which season of the IP this work IS (0 = specials).
    season_number: int = 1
    start_date: date | None = None
    end_date: date | None = None
    content_type: str | None = None
    is_anime: bool | None = None
    manually_edited_fields: list[str] | None = None
    collection_id: str | None = None
    created_at: datetime
    updated_at: datetime
