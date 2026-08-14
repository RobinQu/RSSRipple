"""OrganizePlan ORM 模型。

内置文件整理子系统（organize）的整理计划：一条下载完成通知至多生成
一个计划（``notification_id`` 唯一，即幂等键）。``payload`` 是创建时
冻结的完整通知快照，是执行的唯一依据——通知/作品此后变更或删除都
不影响已生成的计划。``library_id`` 为 null 表示待人工分类的计划，
``category`` 可人工指定电影类别目录。
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OrganizePlan(Base):
    __tablename__ = "organize_plans"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 幂等键：一条下载完成通知至多一个整理计划。通知删除时级联删计划。
    notification_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("download_notifications.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # 命中的规则。规则删除后 SET NULL，计划仍可按冻结快照人工执行。
    rule_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("organize_rules.id", ondelete="SET NULL"),
        nullable=True,
    )
    # null = 待分类计划（尚未确定目标库）。
    library_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("libraries.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 电影类别目录，可人工指定。
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # pending | running | done | failed | cancelled（应用层枚举，见
    # DownloaderInstance.type 的同型注释）。
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True
    )
    # 创建时冻结的完整通知快照，执行的唯一依据。
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    notification = relationship("DownloadNotification")
    rule = relationship("OrganizeRule")
    library = relationship("Library")
    ops = relationship(
        "OrganizePlanOp",
        back_populates="plan",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    audit_entries = relationship(
        "OrganizeAuditEntry",
        back_populates="plan",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
