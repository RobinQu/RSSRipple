"""PendingDecision Pydantic schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class PendingDecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    series_id: str | None = None
    movie_id: str | None = None
    episode: int | None = None
    candidates: list[str]
    reason: str
    llm_suggestion: str | None = None
    decided_resource_id: str | None = None
    status: str
    expires_at: datetime | None = None
    created_at: datetime
    decided_at: datetime | None = None
    updated_at: datetime
    candidate_resources: list[Any] = []
    series: Any | None = None
    movie: Any | None = None


class ConfirmDecisionRequest(BaseModel):
    resource_id: str


class DecisionActionResponse(BaseModel):
    id: str
    status: str
    decided_resource_id: str | None = None
    decided_at: datetime | None = None
