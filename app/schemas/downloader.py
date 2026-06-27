"""DownloaderInstance Pydantic schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DownloaderCreate(BaseModel):
    name: str
    type: str = "transmission"
    url: str
    username: str | None = None
    password: str | None = None
    download_dir: str | None = None


class DownloaderUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    username: str | None = None
    password: str | None = None
    download_dir: str | None = None


class DownloaderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type: str
    url: str
    username: str | None = None
    download_dir: str | None = None
    status: str
    last_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DownloaderTestResult(BaseModel):
    success: bool
    message: str


class TorrentInfo(BaseModel):
    id: int
    name: str
    status: str
    percent_done: float
    rate_download: int
    rate_upload: int
    eta_seconds: int | None = None
    total_size: int
    is_finished: bool


class DownloaderTask(BaseModel):
    id: str
    status: str
    progress: float
    resource_title: str | None = None
    agent_name: str | None = None
