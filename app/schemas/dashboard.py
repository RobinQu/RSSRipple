"""Dashboard Pydantic schemas."""

from typing import Any

from pydantic import BaseModel


class ActiveDownloadTask(BaseModel):
    task_id: str
    resource_title: str
    progress: float
    agent_id: str
    agent_name: str
    channel_id: str
    channel_name: str


class ActiveDownloadGroup(BaseModel):
    type: str
    id: str | None
    title: str
    poster_url: str | None = None
    tasks: list[ActiveDownloadTask] = []


class DashboardData(BaseModel):
    active_agents: int = 0
    active_channels: int = 0
    active_download_count: int = 0
    active_download_groups: list[ActiveDownloadGroup] = []
    pending_decisions: list[Any] = []
