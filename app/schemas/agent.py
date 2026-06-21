"""Agent Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.filter import FilterCreate, FilterResponse


class AgentCreate(BaseModel):
    name: str
    channel_id: str
    downloader_id: str
    download_dir: str | None = None
    task_expire_days: int = 30
    llm_enabled: bool = False
    filters: list[FilterCreate] | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    channel_id: str | None = None
    downloader_id: str | None = None
    download_dir: str | None = None
    task_expire_days: int | None = None
    llm_enabled: bool | None = None
    status: str | None = None


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    channel_id: str
    downloader_id: str
    download_dir: str | None = None
    task_expire_days: int
    llm_enabled: bool
    status: str
    last_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    filters: list[FilterResponse] = []
