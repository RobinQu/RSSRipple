"""WorkCollection Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WorkCollectionCreate(BaseModel):
    title_cn: str
    title_en: str | None = None
    description: str | None = None
    poster_url: str | None = None


class WorkCollectionUpdate(BaseModel):
    title_cn: str | None = None
    title_en: str | None = None
    description: str | None = None
    poster_url: str | None = None


class WorkCollectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title_cn: str
    title_en: str | None = None
    external_id: str | None = None
    external_source: str | None = None
    poster_url: str | None = None
    description: str | None = None
    created_at: datetime
    updated_at: datetime


class WorkCollectionAttach(BaseModel):
    work_type: str  # "series" | "movie"
    work_id: str


class CollectionPart(BaseModel):
    """A TMDB collection part — surfaced on demand, never persisted."""

    tmdb_id: str
    title: str | None = None
    year: int | None = None
    poster_url: str | None = None
