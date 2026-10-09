"""Persisted Agent suggestion groups for unrecognized resources."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.db_types import json_column
from app.utils.sql_time import UTCNow


class AgentSuggestion(Base):
    __tablename__ = "agent_suggestions"
    __table_args__ = (
        UniqueConstraint("agent_id", "sample_title", name="uq_agent_suggestions_title"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    sample_title: Mapped[str] = mapped_column(String(512), nullable=False)
    resources: Mapped[list[str]] = mapped_column(json_column(), default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), onupdate=UTCNow(), nullable=False
    )

    agent = relationship("Agent", back_populates="suggestions")
