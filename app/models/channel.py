"""Channel ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(
        Enum("rss_feed", name="channel_type"), default="rss_feed", nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    fetch_interval: Mapped[int] = mapped_column(Integer, default=1800, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("active", "inactive", "error", name="channel_status"),
        default="active",
        nullable=False,
    )
    field_mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parser_type: Mapped[str] = mapped_column(
        Enum("auto", "mikanani", "custom", name="parser_type"),
        default="auto", nullable=False,
    )
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    file_resources = relationship("FileResource", back_populates="channel", lazy="selectin")
    agents = relationship("Agent", back_populates="channel", lazy="selectin")
