"""PendingDecision Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PendingDecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    episode_id: str | None = None
    movie_id: str | None = None
    candidates: list[str]
    reason: str
    llm_suggestion: str | None = None
    decided_resource_id: str | None = None
    status: str
    created_at: datetime
    decided_at: datetime | None = None


class ConfirmDecisionRequest(BaseModel):
    resource_id: str


class DecisionActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    decided_resource_id: str | None = None
    decided_at: datetime | None = None
