"""DownloaderInstance ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DownloaderInstance(Base):
    __tablename__ = "downloader_instances"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # ``type`` is a plain string with an application-level whitelist rather
    # than a native SQL enum, so adding a new backend (e.g. ``mock``) is a
    # code-only change and works on both SQLite and PostgreSQL without a
    # dedicated migration.
    type: Mapped[str] = mapped_column(
        String(32),
        default="transmission",
        nullable=False,
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    download_dir: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("connected", "disconnected", "error", name="downloader_status"),
        default="disconnected",
        nullable=False,
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    agents = relationship("Agent", back_populates="downloader")
    download_tasks = relationship("DownloadTask", back_populates="downloader")
