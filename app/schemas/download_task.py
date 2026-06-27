"""DownloadTask Pydantic schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DownloadTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    file_resource_id: str
    downloader_id: str | None = None
    download_dir: str | None = None
    transmission_torrent_id: int | None = None
    status: str
    progress: float
    download_speed: int
    upload_speed: int = 0
    eta: int | None = None
    error_message: str | None = None
    retry_count: int
    max_retries: int
    confirmed_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    file_resource: Any | None = None
    agent: Any | None = None


class TaskActionResponse(BaseModel):
    id: str
    status: str
    message: str = ""
