"""FileResource Pydantic schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _coerce_file_size(v: int | float | None) -> int | None:
    """Accept float file_size (e.g. LLM-extracted "10.7 GiB" → 10.7) and truncate to int."""
    if v is None:
        return None
    if isinstance(v, float):
        return int(v)
    return v


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
    title_year: int | None = None
    is_batch: bool = False
    episode_start: int | None = None
    episode_end: int | None = None
    absolute_episode: int | None = None
    episode_confidence: str | None = None
    resolution: str | None = None
    source: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    subtitle_type: str | None = None
    subtitle_langs: list[str] | None = None
    container: str | None = None
    file_size: int | None = None
    torrent_url: str
    detail_url: str | None = None
    published_at: datetime | None = None
    parsed_at: datetime | None = None
    metadata_matched_at: datetime | None = None
    metadata_attempts: int = 0
    last_metadata_attempt_at: datetime | None = None
    metadata_failure_type: str | None = None
    series_id: str | None = None
    movie_id: str | None = None
    audio_work_id: str | None = None
    # Torrent content detection (P1): batch scope sub-classification and the
    # franchise-pack collection link.
    batch_scope: str | None = None
    collection_id: str | None = None
    series: Any | None = None
    movie: Any | None = None
    audio_work: Any | None = None
    # Excluded from the serialized payload; only used to derive
    # ``collection_name`` below. Callers must selectinload the relationship —
    # lazy access under the async session raises MissingGreenlet when
    # ``collection_id`` is non-null.
    collection: Any | None = Field(default=None, exclude=True)
    collection_name: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("file_size", mode="before")
    @classmethod
    def coerce_file_size(cls, v: Any) -> int | None:
        return _coerce_file_size(v)

    @model_validator(mode="after")
    def _fill_collection_name(self) -> "FileResourceResponse":
        if self.collection is not None:
            self.collection_name = self.collection.title_cn or self.collection.title_en
        return self


class GroupedResource(BaseModel):
    type: str
    id: str | None
    title: str
    poster_url: str | None = None
    resources: list[FileResourceResponse] = []


class MetadataSearchRequest(BaseModel):
    search_title: str
    content_type: str = "tv"
    data_source_type: str | None = None


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

    @field_validator("genre", mode="before")
    @classmethod
    def _none_genre_to_empty(cls, v: Any) -> Any:
        # LLM 与本地库候选都可能给出 genre=None（未提供）；响应结构对前端
        # 保持 list 形状，空列表即"未提供"。
        return [] if v is None else v
    status: str | None = None
    number_of_episodes: int | None = None
    number_of_seasons: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    release_date: str | None = None
    runtime: int | None = None


class MetadataLinkRequest(BaseModel):
    selected_result: dict


class EpisodeCorrectionRequest(BaseModel):
    """Payload for PATCH /resources/{id}/episode — manual episode fix.

    ``episode`` is the per-season number the user confirms. ``season`` is
    optional and lets the user fix the season number alongside the episode.
    ``absolute_episode`` is optional and lets the user record the raw
    cross-season number (e.g. 85) so future reconciliation logic sees the
    same evidence. Setting ``episode`` to null clears the value and unmarks
    the confidence tag.
    """

    episode: int | None
    season: int | None = None
    absolute_episode: int | None = None
    note: str | None = None


class ResourceParseCorrectionRequest(BaseModel):
    """Payload for PATCH /resources/{id} — manual correction of parsed fields.

    All fields are optional; only explicitly-sent fields are applied
    (``model_fields_set`` semantics). Invariants enforced server-side
    (mirroring the fetch-service pre-parser):

    - ``is_batch=True`` forces ``episode=None`` and defaults ``batch_scope``
      to ``"season"`` when not explicitly sent.
    - ``is_batch=False`` clears ``batch_scope`` / ``episode_start`` /
      ``episode_end``.
    - Sending any of ``episode`` / ``season`` / ``absolute_episode`` marks
      ``episode_confidence="manual"``.
    """

    episode: int | None = None
    season: int | None = None
    absolute_episode: int | None = None
    episode_start: int | None = None
    episode_end: int | None = None
    is_batch: bool | None = None
    batch_scope: Literal["season", "multi_season", "franchise"] | None = None


class ResourceFileEntry(BaseModel):
    name: str
    size: int


class ResourceFilesResponse(BaseModel):
    """Payload for GET /resources/{id}/files — the torrent's file listing.

    ``source`` records where the listing came from: the local .torrent cache,
    a live .torrent fetch, the downloader RPC, a frozen download-notification
    snapshot, or "none" when no source could produce one.
    """

    files: list[ResourceFileEntry] = []
    source: Literal[
        "torrent_cache", "torrent_fetch", "downloader", "notification", "none"
    ] = "none"

