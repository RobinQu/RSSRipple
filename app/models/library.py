"""Library ORM 模型。

内置文件整理子系统（organize）的目标媒体库，**由媒体服务器扫描派生**
（R2，docs/design/file-organization.md「Library（媒体库，扫描派生）」）：
一个 Library 对应媒体服务器某 section/虚拟目录的一个 Location。库根不再
是静态列，而是结构化卷引用 ``(volume_id, root_subpath)``，使用处动态
解析 ``volume.mount_path + root_subpath``（
``app.services.volume_service.resolve_library_root``）；``volume_id`` 为
NULL = 待绑定，以其为目标的计划落「待绑定」pending，补绑定后可执行。
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Library(Base):
    __tablename__ = "libraries"
    # 扫描派生的幂等键：同一服务器同一 section 的同一 Location 至多一行
    # （media_server_id 为 NULL 的旧数据/手工行不参与唯一约束——NULL 互异）。
    __table_args__ = (
        UniqueConstraint(
            "media_server_id", "section_key", "server_path",
            name="uq_libraries_server_section_path",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 已废弃（P1 手工注册形态，未发布即被 R2 取代）：RSSRipple 进程视角的
    # 静态绝对路径，由 ``volume_id`` / ``root_subpath`` 卷引用取代。列保留
    # 为惰性孤儿，代码不再读取。
    root_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 已废弃：Plex section id 寻址由 ``media_server_id`` + ``section_key``
    # 取代（支持多服务器/多类型）。列保留为惰性孤儿，存量值由轻迁移拷到
    # ``section_key``。
    plex_section: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 来源媒体服务器（扫描派生的来源；SET NULL 保留行——服务器删除后
    # Library 留档为无来源行）。
    media_server_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("media_server_instances.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Plex section key / Emby·Jellyfin 虚拟目录标识（刷新寻址用）。
    section_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 服务器视角原始根路径（bindings 最长前缀匹配的输入，留档）。
    server_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 库根卷引用：解析后 root = volume.mount_path + root_subpath；
    # volume_id NULL = 待绑定。
    volume_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("storage_volumes.id", ondelete="SET NULL"),
        nullable=True,
    )
    root_subpath: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 回收站目录（卷内相对路径，与 root_subpath 同卷）：合集 + move 计划
    # 移走正片后，种子目录内的剩余文件整体移入 ``<卷挂载点>/<recycle_subpath>/
    # <种子目录名>``；NULL = 默认原地保留（keep）。
    recycle_subpath: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # tv | movie | mixed，仅提示性（由 CollectionType 派生），不做应用层强校验。
    kind: Mapped[str] = mapped_column(String(16), default="mixed", nullable=False)
    # BCP-47 语言标签 → Plex 字幕后缀映射（如 {"zh-CN": "zh-Hans"}），可空。
    subtitle_lang_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    media_server = relationship("MediaServerInstance", back_populates="libraries")
    volume = relationship("StorageVolume")
