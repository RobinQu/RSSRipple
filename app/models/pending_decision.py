"""PendingDecision ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.db_types import json_column
from app.utils.sql_time import UTCNow


class PendingDecision(Base):
    __tablename__ = "pending_decisions"
    __table_args__ = (
        CheckConstraint("status != 'pending' OR decision_key IS NOT NULL", name="ck_pending_decision_key"),
        Index("uq_pending_decisions_agent_key", "agent_id", "decision_key", unique=True,
              sqlite_where=text("status = 'pending'"), postgresql_where=text("status = 'pending'")),
        Index("ix_pending_decisions_status_created", "status", "created_at", "id"),
        # Hot FK lookups (P1-D5): work-delete detach and metadata dedup
        # re-point decisions by work FK; agent_id is already covered by the
        # partial unique index prefix above.
        Index("ix_pending_decisions_series_id", "series_id"),
        Index("ix_pending_decisions_movie_id", "movie_id"),
    )

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
    # Season of the disputed episode (NULL = movie / season-less series).
    # Part of the idempotency key so S1E3 and S4E3 don't collide.
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    decision_scope: Mapped[dict | None] = mapped_column(json_column(), nullable=True)
    candidates: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
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
        DateTime, server_default=UTCNow(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), onupdate=UTCNow(), nullable=False
    )

    # Relationships
    agent = relationship("Agent", back_populates="pending_decisions")
    series = relationship("TVSeries", back_populates="pending_decisions")
    movie = relationship("Movie", back_populates="pending_decisions")
