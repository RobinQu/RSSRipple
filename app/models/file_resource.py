"""FileResource ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class FileResource(Base):
    __tablename__ = "file_resources"
    __table_args__ = (
        UniqueConstraint("channel_id", "guid", name="uq_file_resources_channel_guid"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    channel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    guid: Mapped[str] = mapped_column(String(512), nullable=False)
    title_raw: Mapped[str] = mapped_column(String(1024), nullable=False)
    title_cn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title_en: Mapped[str | None] = mapped_column(String(512), nullable=True)
    search_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    subtitle_group: Mapped[str | None] = mapped_column(String(255), nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ── Multi-episode batch (合集) support ──
    # ``is_batch`` marks a torrent that contains multiple episodes (S01E01~13,
    # [01-12 合集], "Season Pack", 全集 …). Batch resources bypass Agent-level
    # per-episode dedup — the current design lets users decide via the filter
    # DSL whether they want singles, batches, or both.
    # ``episode_start`` / ``episode_end`` are best-effort — the raw title may
    # omit the boundaries (e.g. "Batch", "Full Season").
    is_batch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    episode_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ── Cross-season episode reconciliation ──
    # RSS titles sometimes number episodes absolutely across all seasons
    # (e.g. ``S04 - 84`` where 84 = cumulative count across seasons 1-4)
    # rather than per-season. When the MetadataAgent recognizes this via
    # TMDB/Exa ``seasons: [{season_number, episode_count}]`` evidence, it
    # rewrites ``episode`` to the per-season number and preserves the
    # original in ``absolute_episode`` for audit.
    # ``episode_confidence`` records where the value came from:
    #   "raw"          – untouched (title was already per-season, or no
    #                    evidence available).
    #   "reconciled"   – converted from absolute → per-season by the agent.
    #   "ambiguous"    – agent has evidence but couldn't converge; resource
    #                    is routed to AgentSuggestion for manual review.
    #   "manual"       – user corrected via ``PATCH /resources/{id}/episode``.
    #   None           – legacy row created before this column existed.
    absolute_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(100), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subtitle_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # BCP-47 language tags detected on the raw title (best-effort). Sentinel
    # ``["multi"]`` marks titles that only say "multi-language" without
    # spelling out which ones. ``None`` = never populated (legacy row);
    # ``[]`` = parsed but no explicit marking.
    subtitle_langs: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    container: Mapped[str | None] = mapped_column(String(20), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    torrent_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    detail_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Linked entities — set after metadata resolution
    series_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tv_series.id", ondelete="SET NULL"), nullable=True
    )
    movie_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("movies.id", ondelete="SET NULL"), nullable=True
    )
    metadata_matched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    channel = relationship("Channel", back_populates="file_resources")
    series = relationship("TVSeries", back_populates="file_resources")
    movie = relationship("Movie", back_populates="file_resources")
    download_tasks = relationship("DownloadTask", back_populates="file_resource")
