"""DownloadTask Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.file_resource import FileResourceResponse


class DownloadTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    file_resource_id: str
    downloader_id: str
    transmission_torrent_id: int | None = None
    status: str
    progress: float
    download_speed: int
    eta: int | None = None
    error_message: str | None = None
    retry_count: int
    max_retries: int
    confirmed_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    file_resource: FileResourceResponse | None = None


class TaskActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    message: str
