"""PendingDecision ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, JSON, func
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
    episode_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True
    )
    movie_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("movies.id", ondelete="SET NULL"), nullable=True
    )
    candidates: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    reason: Mapped[str] = mapped_column(String(2048), nullable=False)
    llm_suggestion: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    decided_resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("pending", "decided", "expired", "skipped", name="decision_status"),
        default="pending",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    agent = relationship("Agent", back_populates="pending_decisions")
    episode = relationship("Episode", back_populates="pending_decisions")
    movie = relationship("Movie", back_populates="pending_decisions")
