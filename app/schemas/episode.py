"""Episode Pydantic schemas."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class EpisodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    series_id: str
    episode_number: int
    title: str | None = None
    air_date: date | None = None
    preferred_profile: dict | None = None
    created_at: datetime
