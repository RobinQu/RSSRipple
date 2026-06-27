"""FileResource ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
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
    resolution: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(100), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subtitle_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    container: Mapped[str | None] = mapped_column(String(20), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
