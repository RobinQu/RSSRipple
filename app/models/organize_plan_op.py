"""OrganizePlanOp ORM 模型。

整理计划的单文件操作项：计划执行时按 ``seq`` 顺序逐条应用。
``op_type`` 为 move | keep | movedir（keep 无 ``dst``，原地保留只记录；
movedir 表示目录级移动）。每条 op 独立记录状态，单条失败不阻塞其余。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OrganizePlanOp(Base):
    __tablename__ = "organize_plan_ops"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organize_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 计划内的执行顺序。
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # move | keep | movedir；keep 无 dst。
    op_type: Mapped[str] = mapped_column(String(16), nullable=False)
    src: Mapped[str] = mapped_column(String(2048), nullable=False)
    dst: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # 字节数。
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    # pending | done | failed | skipped。
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    plan = relationship("OrganizePlan", back_populates="ops")
