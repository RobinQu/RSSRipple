"""MediaServerInstance / MediaServerBinding ORM 模型。

媒体服务器实例（docs/design/file-organization.md「概念与数据模型」R2）：
取代手工 Library 注册与全局 ``PLEX_*`` 配置，多服务器、多类型
（plex/emby/jellyfin）天然支持。服务器地址/凭证全部入库；Library 由
扫描派生（``POST /media-servers/{id}/scan``），库根经 MediaServerBinding
最长前缀匹配解析为结构化卷引用 ``(volume_id, root_subpath)``。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MediaServerInstance(Base):
    __tablename__ = "media_server_instances"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # plex | emby | jellyfin：应用层白名单（对齐 DownloaderInstance.type
    # 惯例，不用原生 SQL enum，新增类型是纯代码改动）。
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # 访问凭证（Plex token / Emby·Jellyfin API key）明文存 DB，对齐
    # DownloaderInstance.password 惯例；API 响应不回显。
    token: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 停用后不再扫描/刷新，保留行与派生 Library。
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    bindings = relationship(
        "MediaServerBinding",
        back_populates="server",
        cascade="all, delete-orphan",
    )
    libraries = relationship("Library", back_populates="media_server")


class MediaServerBinding(Base):
    """服务器视角路径前缀 → 逻辑卷引用（最长前缀匹配）。

    ``server_path_prefix`` == ``volume.mount_path + subpath``：服务器看到
    的库根路径经此绑定换算为本进程视角。一个服务器可注册多条；无命中 =
    该路径待绑定。卷删除被 volumes API 409 拦截，故 FK 不带 ondelete。
    """

    __tablename__ = "media_server_bindings"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    server_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_server_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 服务器视角路径前缀（如 "/data/Movies"）。
    server_path_prefix: Mapped[str] = mapped_column(String(1024), nullable=False)
    volume_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("storage_volumes.id"), nullable=False
    )
    # 卷内相对路径；"" = 卷根。
    subpath: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    server = relationship("MediaServerInstance", back_populates="bindings")
    volume = relationship("StorageVolume")
