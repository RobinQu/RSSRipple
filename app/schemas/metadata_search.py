"""Public contracts for the unified metadata search and apply workflow."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.services.metadata_source_registry import REGISTRY_SOURCES
from app.services.metadata_sources import SUPPORTED_METADATA_SOURCES

ContentType = Literal["tv", "movie"]
SearchMode = Literal["local", "online"]


class MetadataSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    content_type: ContentType
    mode: SearchMode = "online"
    source: str | None = None
    trusted_sites: list[str] | None = None

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value

    @field_validator("trusted_sites")
    @classmethod
    def _trusted_sites(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        result: list[str] = []
        for raw in value:
            site = str(raw).strip().lower()
            if site not in REGISTRY_SOURCES:
                raise ValueError(f"unsupported trusted site: {raw!r}")
            if site not in result:
                result.append(site)
        return result

    @model_validator(mode="after")
    def _online_requires_source(self) -> MetadataSearchRequest:
        if self.mode == "local":
            if self.source is not None or self.trusted_sites is not None:
                raise ValueError("local search does not accept source or trusted_sites")
            return self
        source = (self.source or "").strip().lower()
        if source not in SUPPORTED_METADATA_SOURCES:
            raise ValueError("online source must be wikipedia, tmdb, or bangumi")
        self.source = source
        return self


class MetadataCandidate(BaseModel):
    """A normalized, typed candidate safe to round-trip for preview/apply."""

    model_config = ConfigDict(extra="forbid")

    origin: Literal["local", "external"]
    content_type: ContentType
    title_cn: str | None = None
    title_en: str | None = None
    original_title: str | None = None
    year: int | None = None
    poster_url: str | None = None
    work_id: str | None = None
    primary_source: str | None = None
    identity_source: str | None = None
    external_id: str | None = None
    match_path: Literal["local", "primary", "web_fallback"]
    selectable: bool = True
    unavailable_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_identity_shape(self) -> MetadataCandidate:
        if self.origin == "local":
            if not self.work_id or self.match_path != "local":
                raise ValueError("local candidate requires work_id and local match_path")
            return self
        if self.primary_source not in SUPPORTED_METADATA_SOURCES:
            raise ValueError("external candidate has an unsupported primary_source")
        if self.selectable and (
            self.identity_source not in REGISTRY_SOURCES or not self.external_id
        ):
            raise ValueError("selectable external candidate requires a trusted identity")
        return self


class WorkMetadataActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    content_type: ContentType
    candidate: MetadataCandidate
    override_manual_edits: bool = False

    @model_validator(mode="after")
    def _candidate_matches_target(self) -> WorkMetadataActionRequest:
        if self.candidate.origin != "external":
            raise ValueError("work metadata can only be refreshed from an external candidate")
        if self.candidate.content_type != self.content_type:
            raise ValueError("candidate content_type does not match target")
        if not self.candidate.selectable:
            raise ValueError("candidate is not selectable")
        return self
