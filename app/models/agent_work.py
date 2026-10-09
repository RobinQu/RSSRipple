"""AgentWork ORM model — per-work subscription within an Agent."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.db_types import json_column
from app.utils.sql_time import UTCNow


class AgentWork(Base):
    __tablename__ = "agent_works"
    __table_args__ = (
        CheckConstraint(
            "(series_id IS NOT NULL AND movie_id IS NULL) OR (series_id IS NULL AND movie_id IS NOT NULL)",
            name="chk_work_single_target",
        ),
        # Hot FK lookups (P1-D5): per-agent subscription listing and per-work
        # subscriber counts; see docs/plans/p0-and-backlog/V28-HOT-FK-INDEXES.md.
        Index("ix_agent_works_agent_id", "agent_id"),
        Index("ix_agent_works_series_id", "series_id"),
        Index("ix_agent_works_movie_id", "movie_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    content_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "tv" | "movie"
    # ON DELETE CASCADE (not SET NULL): the XOR check above requires exactly
    # one target, so SET NULL on work deletion would violate it and block the
    # delete. Deleting a subscribed work cascades its subscription rows.
    series_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tv_series.id", ondelete="CASCADE"), nullable=True
    )
    movie_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("movies.id", ondelete="CASCADE"), nullable=True
    )
    enable_episode_dedup: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    filter_overrides: Mapped[dict | None] = mapped_column(json_column(), nullable=True)
    display_name_override: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), onupdate=UTCNow(), nullable=False
    )

    # Relationships
    agent = relationship("Agent", back_populates="works")
    series = relationship("TVSeries", back_populates="agent_works")
    movie = relationship("Movie", back_populates="agent_works")
