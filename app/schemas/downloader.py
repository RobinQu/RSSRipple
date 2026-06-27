"""DownloaderInstance Pydantic schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.utils.download_paths import validate_download_root


class DownloaderCreate(BaseModel):
    name: str
    type: str = "transmission"
    url: str
    username: str | None = None
    password: str | None = None
    download_dir: str

    def model_post_init(self, __context: Any) -> None:
        self.download_dir = validate_download_root(self.download_dir)


class DownloaderUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    username: str | None = None
    password: str | None = None
    download_dir: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.download_dir is not None:
            self.download_dir = validate_download_root(self.download_dir)


class DownloaderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type: str
    url: str
    username: str | None = None
    download_dir: str
    status: str
    last_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DownloaderTestResult(BaseModel):
    success: bool
    message: str
    version: str | None = None
    free_space: int | None = None


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
