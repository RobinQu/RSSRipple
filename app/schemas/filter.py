"""ResourceFilter Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FilterCreate(BaseModel):
    field: str
    operator: str
    value: str
    priority: int = 0
    is_required: bool = False


class FilterUpdate(BaseModel):
    field: str | None = None
    operator: str | None = None
    value: str | None = None
    priority: int | None = None
    is_required: bool | None = None


class FilterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    field: str
    operator: str
    value: str
    priority: int
    is_required: bool
    created_at: datetime
