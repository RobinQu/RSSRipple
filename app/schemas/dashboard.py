"""Dashboard Pydantic schemas."""

from pydantic import BaseModel

from app.schemas.download_task import DownloadTaskResponse
from app.schemas.pending_decision import PendingDecisionResponse


class DashboardData(BaseModel):
    active_agents: int
    active_downloads: list[DownloadTaskResponse]
    pending_decisions: list[PendingDecisionResponse]
