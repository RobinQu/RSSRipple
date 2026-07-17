"""AudioWork ORM model - non-TV/non-movie works (ASMR, music, drama CD, radio).

Mirrors :class:`Movie` so the upsert / FTS / matching helpers can be reused
with minimal divergence. ``content_type`` discriminates the sub-kind
(``asmr`` / ``music`` / ``drama_cd`` / ``radio`` / ``other``). These works are
resolved via general-purpose search sources only (Wikipedia / Exa) - TMDB has
no coverage for them.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AudioWork(Base):
    __tablename__ = "audio_works"

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
    release_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    runtime: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # asmr | music | drama_cd | radio | other (extensible).
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
    file_resources = relationship("FileResource", back_populates="audio_work")
