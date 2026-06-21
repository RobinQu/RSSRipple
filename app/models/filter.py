"""ResourceFilter ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ResourceFilter(Base):
    __tablename__ = "resource_filters"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    field: Mapped[str] = mapped_column(
        Enum(
            "subtitle_group", "resolution", "container", "video_codec",
            "audio_codec", "subtitle_type", "source", "title_cn", "title_en",
            name="filter_field",
        ),
        nullable=False,
    )
    operator: Mapped[str] = mapped_column(
        Enum("eq", "contains", "fuzzy", "in", "regex", name="filter_operator"),
        nullable=False,
    )
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # Relationships
    agent = relationship("Agent", back_populates="filters")
