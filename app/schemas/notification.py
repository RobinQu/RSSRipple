"""Pydantic schemas for download notifications, per-Agent webhooks and deliveries."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.common import ORMModel
from app.schemas.genre import GenreName

# Placeholder stored for mock webhooks (url column is NOT NULL; mock
# deliveries never perform HTTP so the value is display-only).
MOCK_WEBHOOK_URL = "mock://local"


class WebhookCreateRequest(BaseModel):
    url: str = ""
    mock: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def _url_shape(self) -> "WebhookCreateRequest":
        self.url = self.url.strip()
        if self.mock:
            if not self.url:
                self.url = MOCK_WEBHOOK_URL
        elif not self.url.startswith(("http://", "https://")):
            raise ValueError("webhook url 必须以 http:// 或 https:// 开头")
        return self


class WebhookUpdateRequest(BaseModel):
    url: str | None = None
    mock: bool | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _url_shape(self) -> "WebhookUpdateRequest":
        if self.url is not None:
            self.url = self.url.strip()
            if self.mock:
                # Mock webhooks don't need a URL; empty means "keep current".
                if not self.url:
                    self.url = None
            elif not self.url.startswith(("http://", "https://")):
                raise ValueError("webhook url 必须以 http:// 或 https:// 开头")
        return self


class WebhookOut(ORMModel):
    id: str
    url: str
    mock: bool
    enabled: bool
    created_at: datetime


class DeliverySummary(BaseModel):
    total: int
    done: int
    failed: int
    pending: int


class NotificationListItem(BaseModel):
    id: str
    agent_id: str | None
    download_task_id: str
    # Aggregated across the notification's deliveries: no deliveries or any
    # pending → "pending"; all done → "done"; otherwise → "failed".
    status: str
    delivery_summary: DeliverySummary
    created_at: datetime
    updated_at: datetime


class DeliveryOut(BaseModel):
    id: str
    webhook_id: str
    webhook_url: str | None
    status: str
    attempt_count: int
    error_message: str | None
    delivered_at: datetime | None
    next_attempt_at: datetime | None
    created_at: datetime


class NotificationWorkPayload(BaseModel):
    """Work snapshot inside a notification payload (see build_payload)."""

    type: str | None = None  # "series" | "movie"
    series_id: str | None = None
    movie_id: str | None = None
    title_en: str | None = None
    title_cn: str | None = None
    original_title: str | None = None
    year: int | None = None
    content_type: str | None = None
    collection: str | None = None
    # Closed TMDB genre set (canonical English names) — see GenreName.
    genre: list[GenreName] | None = None
    seasons: list[dict[str, Any]] | None = None
    episodes: list[dict[str, Any]] | None = None


class NotificationAgentPayload(BaseModel):
    id: str
    name: str


class NotificationTaskPayload(BaseModel):
    download_task_id: str
    download_dir: str | None = None
    torrent_name: str | None = None


class NotificationResourcePayload(BaseModel):
    title_raw: str | None = None
    season: int | None = None
    episode: int | None = None
    is_batch: bool = False
    episode_start: int | None = None
    episode_end: int | None = None
    subtitle_langs: list[str] | None = None
    resolution: str | None = None
    container: str | None = None
    title_year: int | None = None


class NotificationPayload(BaseModel):
    """Frozen download.completed snapshot (docs/design/notifications.md).

    All fields optional so snapshots persisted by older versions (which may
    lack newer keys like ``work.genre``) still validate. Unknown keys are
    tolerated for forward compatibility.
    """

    model_config = ConfigDict(extra="allow")

    notification_id: str | None = None
    agent: NotificationAgentPayload | None = None
    task: NotificationTaskPayload | None = None
    resource: NotificationResourcePayload | None = None
    work: NotificationWorkPayload | None = None
    files: list[dict[str, Any]] | None = None


class NotificationDetail(NotificationListItem):
    payload: NotificationPayload
    deliveries: list[DeliveryOut]


class RetryRequest(BaseModel):
    mode: Literal["failed", "all"]
    since: datetime | None = None
    agent_id: str | None = None


class NotificationRetryRequest(BaseModel):
    mode: Literal["failed", "all"]


class RetryResponse(BaseModel):
    reset: int


class RegenerateRequest(BaseModel):
    since: datetime | None = None


class RegenerateResponse(BaseModel):
    created: int
    regenerated: int
