"""Episode ORM model."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, JSON, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    series_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tv_series.id", ondelete="CASCADE"), nullable=False
    )
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    air_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    preferred_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # Relationships
    series = relationship("TVSeries", back_populates="episodes")
    file_resources = relationship("FileResource", back_populates="episode")
    pending_decisions = relationship("PendingDecision", back_populates="episode")
