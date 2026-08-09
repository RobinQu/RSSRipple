"""Pydantic schemas for download notifications and per-Agent webhook registration."""

from datetime import datetime

from pydantic import BaseModel, field_validator

from app.schemas.common import ORMModel


class NotificationListItem(ORMModel):
    id: str
    agent_id: str | None
    download_task_id: str
    status: str
    error_message: str | None
    attempt_count: int
    next_attempt_at: datetime | None
    notified_at: datetime | None
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class NotificationDetail(NotificationListItem):
    payload: dict


class WebhookRegisterRequest(BaseModel):
    url: str | None = None
    mock: bool = False

    @field_validator("url")
    @classmethod
    def _url_shape(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                return None
            if not v.startswith(("http://", "https://")):
                raise ValueError("webhook url 必须以 http:// 或 https:// 开头")
        return v


class WebhookStatusResponse(BaseModel):
    registered: bool
    url: str | None
    mock: bool
    # Per-Agent callback token issued at registration; consumers send it as
    # Bearer on start/ack/fail. Regenerated on every registration.
    token: str | None


class BackfillRequest(BaseModel):
    since: datetime | None = None


class BackfillResponse(BaseModel):
    created: int


class FailRequest(BaseModel):
    error: str
