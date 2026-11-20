from datetime import datetime

from pydantic import BaseModel


class AppLogResource(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    title_raw: str
    title_cn: str | None
    title_en: str | None
    search_title: str | None
    guid: str
    detail_url: str | None
    work_title: str | None
    series_id: str | None
    movie_id: str | None
    collection_id: str | None
    season: int | None
    episode: int | None
    absolute_episode: int | None
    is_batch: bool
    batch_scope: str | None
    metadata_attempts: int
    metadata_failure_type: str | None
    torrent_fetch_status: str | None
    torrent_fetch_error: str | None
    magnet_resolve_status: str | None
    magnet_resolve_error: str | None
    listing_reconcile_status: str | None
    listing_reconcile_error: str | None


class AppLogResponse(BaseModel):
    id: str
    category: str
    severity: str
    message: str
    resource_id: str | None
    channel_id: str | None
    resource_title: str | None = None
    resource: AppLogResource | None = None
    channel_name: str | None = None
    attempts: int
    status: str
    retry_count: int
    last_retried_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
