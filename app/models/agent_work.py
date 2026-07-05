"""AgentWork ORM model — per-work subscription within an Agent."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AgentWork(Base):
    __tablename__ = "agent_works"
    __table_args__ = (
        CheckConstraint(
            "(series_id IS NOT NULL AND movie_id IS NULL) OR (series_id IS NULL AND movie_id IS NOT NULL)",
            name="chk_work_single_target",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    content_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "tv" | "movie"
    series_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tv_series.id", ondelete="SET NULL"), nullable=True
    )
    movie_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("movies.id", ondelete="SET NULL"), nullable=True
    )
    enable_episode_dedup: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    filter_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    display_name_override: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    agent = relationship("Agent", back_populates="works")
    series = relationship("TVSeries", back_populates="agent_works")
    movie = relationship("Movie", back_populates="agent_works")
