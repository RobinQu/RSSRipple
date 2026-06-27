"""DownloadTask ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DownloadTask(Base):
    __tablename__ = "download_tasks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    file_resource_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_resources.id", ondelete="CASCADE"), nullable=False
    )
    downloader_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("downloader_instances.id", ondelete="SET NULL"), nullable=True
    )
    download_dir: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    transmission_torrent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(
            "pending", "queued", "downloading", "paused",
            "completed", "error", "cancelled",
            name="task_status",
        ),
        default="pending",
        nullable=False,
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    download_speed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    upload_speed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    eta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    agent = relationship("Agent", back_populates="download_tasks")
    file_resource = relationship("FileResource", back_populates="download_tasks")
    downloader = relationship("DownloaderInstance", back_populates="download_tasks")
