"""FileResource Pydantic schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.metadata_search import MetadataCandidate


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
    subtitle_groups: list[str] | None = None
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
    # Seasons covered by a multi_season/franchise pack (JSON int list).
    batch_seasons: list[int] | None = None
    # Per-season episode ranges of a batch resource
    # ([{season, episode_start, episode_end}, ...]).
    season_ranges: list[dict] | None = None
    series: Any | None = None
    movie: Any | None = None
    audio_work: Any | None = None
    # Excluded from the serialized payload; only used to derive
    # ``collection_name`` below. Callers must selectinload the relationship —
    # lazy access under the async session raises MissingGreenlet when
    # ``collection_id`` is non-null.
    collection: Any | None = Field(default=None, exclude=True)
    collection_name: str | None = None
    # True once any DownloadTask has ever been created for this resource,
    # regardless of task origin or current status.
    has_download_task: bool = False
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


class ResourceWorkLinkItem(BaseModel):
    """One work association of a (batch) resource.

    ``work_title`` is resolved server-side from the excluded ``series`` /
    ``movie`` inputs — callers must selectinload both relationships.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    series_id: str | None = None
    movie_id: str | None = None
    source: str = "auto"
    series: Any | None = Field(default=None, exclude=True)
    movie: Any | None = Field(default=None, exclude=True)
    work_title: str | None = None
    poster_url: str | None = None

    @model_validator(mode="after")
    def _fill_work_display(self) -> "ResourceWorkLinkItem":
        entity = (
            self.series if self.series_id
            else self.movie if self.movie_id
            else None
        )
        if entity is not None:
            self.work_title = (
                entity.original_title or entity.title_cn or entity.title_en
            )
            self.poster_url = entity.poster_url
        return self


class ResourceFileAssignmentItem(BaseModel):
    """Per-file mapping of a batch resource: torrent file → work/season/run."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    file_path: str
    file_size: int | None = None
    series_id: str | None = None
    movie_id: str | None = None
    work_title_hint: str | None = None
    season: int | None = None
    episode_start: int | None = None
    episode_end: int | None = None
    source: str = "auto"
    series: Any | None = Field(default=None, exclude=True)
    movie: Any | None = Field(default=None, exclude=True)
    work_title: str | None = None

    @model_validator(mode="after")
    def _fill_work_title(self) -> "ResourceFileAssignmentItem":
        entity = (
            self.series if self.series_id
            else self.movie if self.movie_id
            else None
        )
        if entity is not None:
            self.work_title = (
                entity.original_title or entity.title_cn or entity.title_en
            )
        return self


class FileResourceDetailResponse(FileResourceResponse):
    """Detail payload (GET / PATCH / PUT on a single resource) that carries
    the batch enrichment tables. List endpoints use the lean base schema —
    these relationships must be selectinloaded before validating this model
    or lazy access under the async session raises MissingGreenlet."""

    work_links: list[ResourceWorkLinkItem] = []
    file_assignments: list[ResourceFileAssignmentItem] = []
    confirmation_kinds: list[str] = []
    missing_fields: list[str] = []


class GroupedResource(BaseModel):
    type: str
    id: str | None
    title: str
    poster_url: str | None = None
    resources: list[FileResourceResponse] = []


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
    batch_scope: Literal["season", "multi_season", "franchise", "movies"] | None = None
    # Generic media-descriptor corrections (wizard step 3). Only explicitly
    # sent keys are applied; these never touch ``episode_confidence``.
    resolution: str | None = None
    subtitle_group: str | None = None
    subtitle_groups: list[str] | None = None
    source: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    subtitle_type: str | None = None
    container: str | None = None
    subtitle_langs: list[str] | None = None


class AssociationWorkRef(BaseModel):
    """One work of a resource's association set (PUT /resources/{id}/associations)."""

    work_type: Literal["series", "movie"]
    work_id: str | None = None
    client_key: str | None = None
    candidate: MetadataCandidate | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "AssociationWorkRef":
        if self.candidate is not None:
            if not self.client_key:
                raise ValueError("client_key is required for an external candidate")
            if self.work_id not in (None, self.client_key):
                raise ValueError("candidate work_id must be its client_key")
            self.work_id = None
            expected = "tv" if self.work_type == "series" else "movie"
            if (
                self.candidate.origin != "external"
                or self.candidate.content_type != expected
                or not self.candidate.selectable
            ):
                raise ValueError("candidate does not match work_type")
        elif self.work_id is None:
            raise ValueError("work_id is required for an existing work")
        return self


class AssociationFileAssignment(BaseModel):
    """One file→work placement inside the association update payload."""

    file_path: str
    work_type: Literal["series", "movie"]
    work_id: str
    file_size: int | None = None
    season: int | None = None
    episode_start: int | None = None
    episode_end: int | None = None

    @model_validator(mode="after")
    def _check_range(self) -> "AssociationFileAssignment":
        if (
            self.episode_start is not None
            and self.episode_end is not None
            and self.episode_start > self.episode_end
        ):
            raise ValueError("episode_start must be <= episode_end")
        return self


class ResourceAssociationUpdateRequest(BaseModel):
    """Payload for PUT /resources/{id}/associations — the edit wizard's full
    desired state, replacing the resource's association set atomically.

    Invariants enforced server-side (see resources.apply_association_update):

    - ``is_batch=False``: at most one work — written to the legacy mutually
      exclusive FK; links / assignments / collection are cleared.
    - ``is_batch=True`` with exactly one TV work: that work also lands in the
      legacy FK so the agent dedup coverage key keeps working.
    - ``is_batch=True`` with 2+ works or any movie pack: legacy work FKs are
      cleared and only the link table carries associations.
    - ``batch_scope`` may be omitted — it is derived from the works set:
      all-movie → "movies"; mixed tv+movie or multi-tv → "franchise";
      single tv → "season" when a season is known else "multi_season".
    - Every assignment's (work_type, work_id) must appear in ``works``.
    """

    is_batch: bool
    works: list[AssociationWorkRef] = []
    collection_id: str | None = None
    assignments: list[AssociationFileAssignment] = []
    # Single-episode fields for the non-batch branch (applied only when
    # ``is_batch=False`` and explicitly sent; marks episode_confidence manual).
    season: int | None = None
    episode: int | None = None
    absolute_episode: int | None = None
    # Generic media-descriptor corrections applied in the same transaction.
    fields: dict[str, Any] | None = None


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
