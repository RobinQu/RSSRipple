"""ResourceFileAssignment ORM model — per-file work/season/episode mapping."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ResourceFileAssignment(Base):
    __tablename__ = "resource_file_assignments"
    __table_args__ = (
        UniqueConstraint("resource_id", "file_path", name="uq_assignment_resource_path"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    resource_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_resources.id", ondelete="CASCADE"), nullable=False
    )
    # Relative path exactly as it appears in the torrent listing (the same
    # identity ``GET /resources/{id}/files`` returns), so assignments survive
    # re-fetches of the listing and line up with organize snapshots.
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Denormalized snapshot of the listing entry size (display convenience).
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Bound work. Both NULL = unassigned placement (deterministic/LLM layers
    # run before the resource is linked to works); app-level validation keeps
    # the mutual exclusivity when a work IS bound.
    series_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tv_series.id", ondelete="CASCADE"), nullable=True
    )
    movie_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("movies.id", ondelete="CASCADE"), nullable=True
    )
    # Work title this placement belongs to before a concrete work row binds
    # (top-level cluster dir from the torrent analysis, or the LLM-suggested
    # movie title). Lets the wizard bind suggestions to step-1 works by title.
    work_title_hint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # TV-only placement: which season this file belongs to and the per-season
    # episode run it covers. A single-episode file has start == end; a file
    # spanning several episodes (e.g. "E01-02") uses the inclusive range.
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Provenance: "auto" (deterministic path analysis), "llm" (batch content
    # analysis) or "manual" (edit wizard). Automatic layers never overwrite
    # rows whose source is "manual".
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    resource = relationship("FileResource", back_populates="file_assignments")
    series = relationship("TVSeries")
    movie = relationship("Movie")
