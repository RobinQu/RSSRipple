"""Movie Pydantic schemas."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.genre import GenreName


class MovieCreate(BaseModel):
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
    release_date: date | None = None
    runtime: int | None = None
    content_type: str | None = "movie"
    is_anime: bool | None = None


class MovieUpdate(BaseModel):
    title_cn: str | None = None
    title_en: str | None = None
    original_title: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    poster_url: str | None = None
    rating: float | None = None
    genre: list[GenreName] | None = None
    status: str | None = None
    release_date: date | None = None
    runtime: int | None = None
    is_anime: bool | None = None


class MovieResponse(BaseModel):
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
    release_date: date | None = None
    runtime: int | None = None
    content_type: str | None = None
    is_anime: bool | None = None
    manually_edited_fields: list[str] | None = None
    collection_id: str | None = None
    created_at: datetime
    updated_at: datetime
