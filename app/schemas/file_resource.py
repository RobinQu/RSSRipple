"""FileResource Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FileResourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    channel_id: str
    guid: str
    title_raw: str
    title_cn: str | None = None
    title_en: str | None = None
    subtitle_group: str | None = None
    episode: int | None = None
    resolution: str | None = None
    source: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    subtitle_type: str | None = None
    container: str | None = None
    file_size: int | None = None
    torrent_url: str
    detail_url: str | None = None
    published_at: datetime | None = None
    created_at: datetime
