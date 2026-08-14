"""DownloaderInstance ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, func
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
    # 已废弃（P1 未发布即被 R1 取代）：daemon 视角 → 本进程视角的自由文本
    # 前缀字典，由 ``volume_id`` / ``volume_subpath`` 卷绑定取代。列保留为
    # 惰性孤儿，代码不再读取。
    path_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 卷绑定（R1）：daemon 视角的 ``download_dir`` 根 ==
    # ``volume.mount_path + volume_subpath``；两者皆 null = 两视角一致
    # （恒等，现状默认）。解析走
    # ``app.services.volume_service.resolve_downloader_path``。
    volume_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("storage_volumes.id", ondelete="SET NULL"),
        nullable=True,
    )
    volume_subpath: Mapped[str | None] = mapped_column(String(1024), nullable=True)
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
    volume = relationship("StorageVolume")
    agents = relationship("Agent", back_populates="downloader")
    download_tasks = relationship("DownloadTask", back_populates="downloader")
