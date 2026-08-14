"""StorageVolume Pydantic schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.utils.download_paths import validate_download_root


class StorageVolumeCreate(BaseModel):
    name: str
    mount_path: str
    remark: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if not self.name.strip():
            raise ValueError("name is required")
        self.mount_path = validate_download_root(self.mount_path)


class StorageVolumeUpdate(BaseModel):
    name: str | None = None
    mount_path: str | None = None
    remark: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.name is not None and not self.name.strip():
            raise ValueError("name must not be empty")
        if self.mount_path is not None:
            self.mount_path = validate_download_root(self.mount_path)


class StorageVolumeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    mount_path: str
    remark: str | None = None
    created_at: datetime
    updated_at: datetime


class StorageVolumeCheckResult(BaseModel):
    exists: bool
    writable: bool
