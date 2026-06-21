"""DownloaderInstance Pydantic schemas."""

from datetime import datetime

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


class TestConnectionResponse(BaseModel):
    success: bool
    message: str
    version: str | None = None
