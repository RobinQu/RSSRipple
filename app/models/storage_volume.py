"""StorageVolume ORM 模型。

逻辑存储卷（docs/design/file-organization.md「统一路径解析：逻辑存储卷」）：
用户声明的逻辑卷，指向 RSSRipple 容器内一个挂载点（compose 启动时把宿主/
远程存储挂进来）。一切配置面路径引用（下载器卷绑定、媒体服务器绑定、
Library 库根）一律存 ``(volume_id, subpath)``，不落库绝对路径；使用处
动态解析 ``volume.mount_path + subpath``——挂载点改了一处修改全局生效。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StorageVolume(Base):
    __tablename__ = "storage_volumes"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 显示名（如「flash-aio」「local」），全局唯一。
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # RSSRipple 容器内绝对路径（docker/compose 启动时挂载宿主/远程存储到此）；
    # 保存时探测存在性（不存在 422），写权限探测仅作展示提示。
    mount_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    remark: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
