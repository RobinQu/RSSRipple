"""DownloadTask Pydantic schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field, model_validator

from app.schemas.file_resource import FileResourceResponse


class DownloadTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str | None = None
    file_resource_id: str
    downloader_id: str | None = None
    download_dir: str | None = None
    keep_seeding: bool
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
    file_resource: FileResourceResponse | None = None
    agent: Any | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def auto_delete_on_completion(self) -> bool:
        """Deprecated inverse alias retained for older API clients."""
        return not self.keep_seeding


class ManualTaskCreate(BaseModel):
    """Manual download task creation (bypasses agents)."""

    resource_id: str
    downloader_id: str
    keep_seeding: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_auto_delete(cls, data: Any) -> Any:
        from app.schemas.downloader import _translate_legacy_keep_seeding

        return _translate_legacy_keep_seeding(data)


class TaskActionResponse(BaseModel):
    id: str
    status: str
    message: str = ""


class BatchTaskRetryRequest(BaseModel):
    """Batch retry request; ``task_ids=None`` retries all retryable tasks of the agent."""

    task_ids: list[str] | None = None


class BatchTaskRetryResponse(BaseModel):
    processed: int = 0
    retried: int = 0
    failed: int = 0
    errors: list[str] = []
