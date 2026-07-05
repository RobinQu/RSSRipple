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
    conflict_resolution: str = "ask"
    filter_config: dict | None = None
    status: str = "active"
    works: list[AgentWorkCreate] = []

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
    filter_config: dict | None = None
    status: str | None = None
    works: list[AgentWorkCreate] | None = None

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
    filter_config: dict | None = None
    status: str
    last_run_at: datetime | None = None
    last_run_status: str | None = None
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
