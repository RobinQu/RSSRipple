"""Agent Pydantic schemas."""

import json as _json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from app.schemas.common import ORMModel
from app.utils.download_paths import validate_download_subdir


class AgentWorkCreate(BaseModel):
    content_type: str  # "tv" | "movie"
    series_id: str | None = None
    movie_id: str | None = None
    enable_episode_dedup: bool = True
    filter_overrides: dict | None = None
    display_name_override: str | None = None


class AgentWorkUpdate(BaseModel):
    enable_episode_dedup: bool | None = None
    filter_overrides: dict | None = None
    display_name_override: str | None = None


class AgentWorkResponse(ORMModel):
    id: str
    agent_id: str
    content_type: str
    series_id: str | None = None
    movie_id: str | None = None
    enable_episode_dedup: bool = True
    filter_overrides: dict | None = None
    display_name_override: str | None = None
    series: Any | None = None
    movie: Any | None = None
    created_at: datetime
    updated_at: datetime


class AgentCreate(BaseModel):
    name: str
    channel_id: str
    downloader_id: str
    download_subdir: str | None = None
    task_expire_days: int = 30
    llm_enabled: bool = False
    scope_channel_wide: bool = False
    conflict_resolution: str = "auto"
    llm_prompt: str | None = None
    filter_config: dict | None = None
    status: str = "active"
    works: list[AgentWorkCreate] = []
    # Resource ids the user selected from the rules-preview diff to backfill
    # into the download queue. Sent by AgentForm after the preview modal; when
    # present (even if empty) the agent's watermark is advanced to the channel
    # max so future delta runs only see truly new resources. None = plain save
    # with no backfill / watermark change (non-rule edits).
    dispatch_resource_ids: list[str] | None = None

    @model_validator(mode="after")
    def _validate_download_subdir(self):
        self.download_subdir = validate_download_subdir(self.download_subdir)
        return self


class AgentUpdate(BaseModel):
    name: str | None = None
    channel_id: str | None = None
    downloader_id: str | None = None
    download_subdir: str | None = None
    task_expire_days: int | None = None
    llm_enabled: bool | None = None
    scope_channel_wide: bool | None = None
    conflict_resolution: str | None = None
    llm_prompt: str | None = None
    filter_config: dict | None = None
    status: str | None = None
    works: list[AgentWorkCreate] | None = None
    # See AgentCreate.dispatch_resource_ids. When present, the rules change is
    # treated as a backfill commit: dispatch the selected resources and
    # advance the watermark.
    dispatch_resource_ids: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _decode_bytes(cls, v):
        if isinstance(v, (bytes, bytearray)):
            return _json.loads(v)
        return v

    @model_validator(mode="after")
    def _validate_download_subdir(self):
        self.download_subdir = validate_download_subdir(self.download_subdir)
        return self


class AgentResponse(ORMModel):
    id: str
    name: str
    channel_id: str
    downloader_id: str
    download_subdir: str | None = None
    task_expire_days: int
    llm_enabled: bool
    scope_channel_wide: bool
    conflict_resolution: str
    llm_prompt: str | None = None
    filter_config: dict | None = None
    status: str
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    last_consumed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    works: list[AgentWorkResponse] = []
    channel: Any | None = None
    downloader: Any | None = None


class AgentListItem(AgentResponse):
    channel_name: str | None = None
    downloader_name: str | None = None
    active_task_count: int = 0


class TestFilterResourceResult(BaseModel):
    resource_id: str
    title_raw: str
    passed: bool
    condition_results: list[dict] = []


class TestFilterResult(BaseModel):
    resources: list[TestFilterResourceResult] = []
    total: int = 0
    passed: int = 0


class SuggestionGroup(BaseModel):
    id: str | None = None
    sample_title: str
    resources: list[str] = []
    status: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RunResponse(BaseModel):
    task_id: str | None = None


class RulesPreviewRequest(BaseModel):
    """Proposed subscription rules to diff against an agent's current rules.

    For an existing agent, ``agent_id`` is set and the old rules are read from
    the DB. For create (no agent yet), ``channel_id`` is set and old rules are
    treated as empty (everything matching is newly-matching).
    """
    agent_id: str | None = None
    channel_id: str | None = None
    scope_channel_wide: bool = False
    filter_config: dict | None = None
    works: list[AgentWorkCreate] = []


class RulesPreviewResource(ORMModel):
    id: str
    title_raw: str
    title_cn: str | None = None
    subtitle_group: str | None = None
    resolution: str | None = None
    source: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    subtitle_type: str | None = None
    subtitle_langs: list[str] | None = None
    container: str | None = None
    file_size: int | None = None
    episode: int | None = None
    season: int | None = None
    episode_confidence: str | None = None
    published_at: datetime | None = None
    series_id: str | None = None
    movie_id: str | None = None


class RulesPreviewResponse(BaseModel):
    newly_matching: list[RulesPreviewResource] = []
    no_longer_matching: list[RulesPreviewResource] = []
    in_queue_skipped: int = 0


class AgentRunResource(ORMModel):
    """Lightweight resource summary embedded in a run record for display."""
    id: str
    title_raw: str
    title_cn: str | None = None
    subtitle_group: str | None = None
    resolution: str | None = None
    episode: int | None = None
    season: int | None = None


class AgentRunResponse(ORMModel):
    id: str
    agent_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    total_resources: int
    matched: int
    dispatched: int
    pending_decisions: int
    filter_failed: int
    duplicates_skipped: int
    unrecognized: int
    matched_resource_ids: list[str] = []
    errors: list[str] = []
    # Full resource summaries (same shape as the rules-preview diff) so the
    # run-history drawer can show rich metadata without an extra fetch.
    matched_resources: list[RulesPreviewResource] = []


class RunStatusResponse(BaseModel):
    job_id: str | None = None
    status: str | None = None
    result: dict | None = None
    error: str | None = None
    queued_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    result: Any = None
    error: str | None = None
