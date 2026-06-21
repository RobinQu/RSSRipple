"""Agent ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, func
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
        String(36), ForeignKey("downloader_instances.id", ondelete="SET NULL"), nullable=True
    )
    download_dir: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    task_expire_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    llm_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_source: Mapped[str | None] = mapped_column(
        Enum("tmdb", "tvdb", "none", name="metadata_source"),
        nullable=True, default=None,
    )
    content_type: Mapped[str] = mapped_column(
        Enum("anime", "tv", "movie", "mixed", name="content_type"),
        default="anime", nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Enum("active", "paused", "error", name="agent_status"),
        default="active",
        nullable=False,
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    channel = relationship("Channel", back_populates="agents")
    downloader = relationship("DownloaderInstance", back_populates="agents")
    filters = relationship(
        "ResourceFilter", back_populates="agent",
        order_by="ResourceFilter.priority.desc()",
        lazy="selectin",
    )
    download_tasks = relationship("DownloadTask", back_populates="agent", lazy="selectin")
    pending_decisions = relationship("PendingDecision", back_populates="agent", lazy="selectin")
