"""AgentRun ORM model — a persisted record of a single agent execution."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # "running" | "success" | "failed" | "pending_decisions"
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    total_resources: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dispatched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_decisions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    filter_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unrecognized: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Resource ids that matched the agent's rules this run (passed work-scope
    # + filter). Shown in the run-history "matched resources" list.
    matched_resource_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    errors: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    agent = relationship("Agent", back_populates="runs")
