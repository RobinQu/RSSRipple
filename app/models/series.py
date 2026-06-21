"""TVSeries ORM model."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, JSON, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TVSeries(Base):
    __tablename__ = "tv_series"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title_cn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title_en: Mapped[str | None] = mapped_column(String(512), nullable=True)
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    external_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    genre: Mapped[list | None] = mapped_column(JSON, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    episodes = relationship("Episode", back_populates="series", order_by="Episode.episode_number")
