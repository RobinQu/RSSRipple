"""AgentRun ORM model — a persisted record of a single agent execution."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.db_types import json_column
from app.utils.sql_time import UTCNow


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        # Run-history API filters by agent and pages ORDER BY started_at DESC;
        # the composite serves both the filter and the ordering (P1-D5).
        Index("ix_agent_runs_agent_started", "agent_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # "running" | "success" | "failed" | "pending_decisions"
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    # Scan-window lower bound for manual windowed runs (scenario ④). NULL for
    # delta/targeted runs; 1970-01-01 marks an explicit "no limit" full scan.
    scan_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_resources: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dispatched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_decisions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    filter_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unrecognized: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Resource ids that matched the agent's rules this run (passed work-scope
    # + filter). Shown in the run-history "matched resources" list.
    matched_resource_ids: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
    errors: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), nullable=False
    )

    agent = relationship("Agent", back_populates="runs")
