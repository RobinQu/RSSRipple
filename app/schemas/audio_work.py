"""AudioWork Pydantic schemas - non-TV/non-movie works (ASMR / music / ...)."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class AudioWorkCreate(BaseModel):
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
    release_date: date | None = None
    runtime: int | None = None
    content_type: str | None = "other"


class AudioWorkUpdate(BaseModel):
    title_cn: str | None = None
    title_en: str | None = None
    original_title: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    poster_url: str | None = None
    rating: float | None = None
    genre: list[str] | None = None
    status: str | None = None
    release_date: date | None = None
    runtime: int | None = None
    content_type: str | None = None


class AudioWorkResponse(BaseModel):
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
    release_date: date | None = None
    runtime: int | None = None
    content_type: str | None = None
    wikipedia_url: str | None = None
    wikipedia_page_id: int | None = None
    created_at: datetime
    updated_at: datetime
