"""Metadata cache for title cleaning and agent results.

Stores cached results keyed by ``(title, source)`` where ``source`` indicates
the cache type:

- ``"llm_title"`` — Legacy title cleaning cache (pre-refactor, retained for reference).
- ``"metadata_agent"`` — Full metadata agent result, including clean_title,
  content_type, inferred episode/season, matched entity, and confidence.

The ``metadata_json`` column stores the complete result dict, whose shape
depends on the ``source`` value.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MetadataCache(Base):
    __tablename__ = "metadata_cache"
    __table_args__ = (
        UniqueConstraint("title", "source", name="uq_metadata_cache_key"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    content_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
