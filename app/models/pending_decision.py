"""PendingDecision ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PendingDecision(Base):
    __tablename__ = "pending_decisions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    series_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tv_series.id", ondelete="SET NULL"), nullable=True
    )
    movie_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("movies.id", ondelete="SET NULL"), nullable=True
    )
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candidates: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    reason: Mapped[str] = mapped_column(String(2048), nullable=False)
    llm_suggestion: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # The candidate the LLM picked (resource id), if any. Drives the
    # "AI auto-handle" action and the highlighted row in the decisions UI.
    llm_picked_resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("pending", "decided", "expired", "skipped", name="decision_status"),
        default="pending",
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    agent = relationship("Agent", back_populates="pending_decisions")
    series = relationship("TVSeries", back_populates="pending_decisions")
    movie = relationship("Movie", back_populates="pending_decisions")
