"""OrganizeAuditEntry ORM 模型。

整理计划的审计日志：计划生命周期内的每次动作（创建、确认、执行、
失败、取消等）追加一条，只增不改。``detail`` 为自由结构 JSON 快照。
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OrganizeAuditEntry(Base):
    __tablename__ = "organize_audit_entries"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organize_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # Relationships
    plan = relationship("OrganizePlan", back_populates="audit_entries")
