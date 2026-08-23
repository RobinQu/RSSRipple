"""ResourceWorkLink ORM model — multi-work association for batch resources."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ResourceWorkLink(Base):
    __tablename__ = "resource_work_links"
    __table_args__ = (
        CheckConstraint(
            "(series_id IS NOT NULL AND movie_id IS NULL) OR (series_id IS NULL AND movie_id IS NOT NULL)",
            name="chk_resource_link_single_target",
        ),
        UniqueConstraint("resource_id", "series_id", name="uq_resource_link_series"),
        UniqueConstraint("resource_id", "movie_id", name="uq_resource_link_movie"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    resource_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_resources.id", ondelete="CASCADE"), nullable=False
    )
    series_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tv_series.id", ondelete="CASCADE"), nullable=True
    )
    movie_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("movies.id", ondelete="CASCADE"), nullable=True
    )
    # Provenance of this association: deterministic pipeline ("auto"), LLM
    # batch-content analysis ("llm") or the manual edit wizard ("manual").
    # Automatic layers never overwrite rows whose source is "manual".
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    resource = relationship("FileResource", back_populates="work_links")
    series = relationship("TVSeries")
    movie = relationship("Movie")
