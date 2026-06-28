"""LLM title dedup cache.

Stores LLM-extracted clean titles keyed by ``(title, source="llm_title")`` so
that repeated ``clean_title_llm`` calls for the same raw title (e.g. different
episodes of the same series) only hit the LLM once per process restart.

External metadata (TMDB results) is cached in the ``movies`` and
``tv_series`` tables — not here.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, JSON, UniqueConstraint, func
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
