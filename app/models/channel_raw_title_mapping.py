"""ChannelRawTitleMapping ORM model — user-corrected raw title → work mappings."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ChannelRawTitleMapping(Base):
    __tablename__ = "channel_raw_title_mappings"
    __table_args__ = (
        UniqueConstraint("channel_id", "search_title_key", name="uq_channel_search_key"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    channel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    raw_title: Mapped[str] = mapped_column(String(1024), nullable=False)
    search_title_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    search_title_override: Mapped[str | None] = mapped_column(String(512), nullable=True)
    series_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tv_series.id", ondelete="SET NULL"), nullable=True
    )
    movie_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("movies.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    channel = relationship("Channel", back_populates="raw_title_mappings")
    series = relationship("TVSeries", back_populates="raw_title_mappings")
    movie = relationship("Movie", back_populates="raw_title_mappings")
