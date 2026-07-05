"""TVSeries ORM model."""

import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TVSeries(Base):
    __tablename__ = "tv_series"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title_cn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title_en: Mapped[str | None] = mapped_column(String(512), nullable=True)
    original_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    external_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    poster_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    genre: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    number_of_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    number_of_seasons: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    canonical_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    wikipedia_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    wikipedia_page_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    episodes = relationship(
        "Episode", back_populates="series", order_by="Episode.season.asc(), Episode.episode.asc()",
        cascade="all, delete-orphan",
    )
    file_resources = relationship("FileResource", back_populates="series")
    agent_works = relationship(
        "AgentWork", back_populates="series"
    )
    raw_title_mappings = relationship(
        "ChannelRawTitleMapping", back_populates="series"
    )
    pending_decisions = relationship(
        "PendingDecision", back_populates="series"
    )
