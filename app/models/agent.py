"""Agent ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    downloader_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("downloader_instances.id", ondelete="RESTRICT"), nullable=False
    )
    download_subdir: Mapped[str | None] = mapped_column(String(500), nullable=True)
    task_expire_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    llm_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    scope_channel_wide: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    conflict_resolution: Mapped[str] = mapped_column(
        String(20), default="auto", nullable=False
    )
    # Optional user-supplied instruction for the LLM candidate picker. When
    # empty the built-in default prompt is used (prefer most-complete
    # metadata, highest resolution, subtitles, newest). Applies to both the
    # "auto" conflict-resolution path and the suggestion shown in "ask" mode.
    llm_prompt: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    filter_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("active", "paused", "error", name="agent_status"),
        default="active",
        nullable=False,
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Consumption watermark: the latest ``FileResource.created_at`` timestamp
    # this agent has already considered. Delta runs (fetch-triggered / manual)
    # only process resources with ``created_at > last_consumed_at``; rule-change
    # saves advance it to the channel's current max so subsequent delta runs
    # only see truly new resources. Null = never run (treated as "process
    # nothing, set to now" to avoid silently auto-dispatching backfill —
    # backfill must go through the rules-preview selection flow).
    last_consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    channel = relationship("Channel", back_populates="agents")
    downloader = relationship("DownloaderInstance", back_populates="agents")
    works = relationship(
        "AgentWork",
        back_populates="agent",
        order_by="AgentWork.created_at.asc()",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    download_tasks = relationship(
        "DownloadTask",
        back_populates="agent",
        lazy="selectin",
        passive_deletes=True,
    )
    pending_decisions = relationship(
        "PendingDecision",
        back_populates="agent",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    suggestions = relationship(
        "AgentSuggestion",
        back_populates="agent",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    runs = relationship(
        "AgentRun",
        back_populates="agent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
