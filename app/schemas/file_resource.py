"""FileResource Pydantic schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class FileResourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    channel_id: str
    guid: str
    title_raw: str
    title_cn: str | None = None
    title_en: str | None = None
    search_title: str | None = None
    subtitle_group: str | None = None
    episode: int | None = None
    season: int | None = None
    resolution: str | None = None
    source: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    subtitle_type: str | None = None
    container: str | None = None
    file_size: int | None = None
    torrent_url: str
    detail_url: str | None = None
    published_at: datetime | None = None
    parsed_at: datetime | None = None
    metadata_matched_at: datetime | None = None
    series_id: str | None = None
    movie_id: str | None = None
    series: Any | None = None
    movie: Any | None = None
    created_at: datetime
    updated_at: datetime


class GroupedResource(BaseModel):
    type: str
    id: str | None
    title: str
    poster_url: str | None = None
    resources: list[FileResourceResponse] = []


class MetadataSearchRequest(BaseModel):
    search_title: str
    content_type: str = "tv"


class MetadataSearchResult(BaseModel):
    content_type: str
    title_cn: str | None = None
    title_en: str | None = None
    original_title: str | None = None
    description: str | None = None
    poster_url: str | None = None
    year: int | None = None
    external_id: str | None = None
    rating: float | None = None
    genre: list[str] = []
    status: str | None = None
    number_of_episodes: int | None = None
    number_of_seasons: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    release_date: str | None = None
    runtime: int | None = None


class MetadataLinkRequest(BaseModel):
    selected_result: dict
