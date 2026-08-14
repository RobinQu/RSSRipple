"""DownloaderInstance Pydantic schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.utils.download_paths import validate_download_root, validate_download_subdir

DownloaderType = Literal["transmission", "mock"]


def _validate_volume_subpath(value: str | None) -> str | None:
    """卷内子路径：相对路径校验（空串归 null），规则同 Agent.download_subdir。"""
    return validate_download_subdir(value)


class DownloaderCreate(BaseModel):
    name: str
    type: DownloaderType = "transmission"
    url: str | None = None
    username: str | None = None
    password: str | None = None
    download_dir: str | None = None
    # 卷绑定（R1）：daemon 视角 download_dir 根 == volume.mount_path +
    # volume_subpath；两者皆 null = 两视角一致（恒等）。
    volume_id: str | None = None
    volume_subpath: str | None = None

    def model_post_init(self, __context: Any) -> None:
        # Mock downloaders don't talk to any real service; provide sane
        # defaults so the API stays uniform (url/download_dir are still
        # non-null in the DB — see the ORM ``nullable=False``).
        if self.type == "mock":
            if not self.url:
                self.url = "mock://local"
            if not self.download_dir:
                self.download_dir = "/tmp/mock-downloads"
        if not self.url:
            raise ValueError("url is required")
        if not self.download_dir:
            raise ValueError("download_dir is required")
        # download_dir validation only applies to real (server-writable) paths.
        if self.type != "mock":
            self.download_dir = validate_download_root(self.download_dir)
        self.volume_subpath = _validate_volume_subpath(self.volume_subpath)


class DownloaderUpdate(BaseModel):
    name: str | None = None
    type: DownloaderType | None = None
    url: str | None = None
    username: str | None = None
    password: str | None = None
    download_dir: str | None = None
    volume_id: str | None = None
    volume_subpath: str | None = None

    def model_post_init(self, __context: Any) -> None:
        # Only validate the root when the *incoming* payload declares a real
        # (non-mock) downloader. For mock updates we accept anything.
        if self.download_dir is not None and self.type != "mock":
            self.download_dir = validate_download_root(self.download_dir)
        self.volume_subpath = _validate_volume_subpath(self.volume_subpath)


class DownloaderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type: str
    url: str
    username: str | None = None
    download_dir: str
    volume_id: str | None = None
    volume_subpath: str | None = None
    status: str
    last_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DownloaderTestResult(BaseModel):
    success: bool
    message: str
    version: str | None = None
    free_space: int | None = None


class DownloaderTestRequest(BaseModel):
    """Optional overrides for ``POST /downloaders/{id}/test``.

    Lets the edit form probe the *unsaved* form values instead of the stored
    config. Any field left null falls back to the stored value — notably a
    blank password means "keep the stored one" (the form never echoes it).
    """

    url: str | None = None
    username: str | None = None
    password: str | None = None
    download_dir: str | None = None


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
