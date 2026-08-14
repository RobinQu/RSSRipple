"""OrganizeRule ORM 模型。

内置文件整理子系统（organize）的归类规则。规则按 ``priority`` 升序
first-match-wins：``filter``（BoolCondition DSL 根节点 JSON，null=匹配
全部）命中下载完成通知后，按 ``path_template`` 计算目标路径，把文件
整理进 ``library_id`` 指向的 Library。删除 Library 受 RESTRICT 保护：
仍有规则引用时不允许删。
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OrganizeRule(Base):
    __tablename__ = "organize_rules"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 小值在前，first-match-wins。
    priority: Mapped[int] = mapped_column(
        Integer, default=100, nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # BoolCondition DSL 根节点 JSON；null = 匹配全部通知。
    filter: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    library_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("libraries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    path_template: Mapped[str] = mapped_column(String(1024), nullable=False)
    # move（默认）| hardlink | copy；hardlink/copy 为保种模式（源文件保留，
    # 执行后不删任务、恢复快照时停过的做种）。
    file_op: Mapped[str] = mapped_column(String(16), default="move", nullable=False)
    # True = 计划创建后无需人工确认直接执行；False = 人工在队列里确认。
    auto_execute: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
