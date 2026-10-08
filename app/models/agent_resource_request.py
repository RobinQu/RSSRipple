"""Durable requests to reconsider a resource using the Agent's current rules."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.sql_time import UTCNow


class AgentResourceRequest(Base):
    __tablename__ = "agent_resource_requests"
    __table_args__ = (UniqueConstraint("agent_id", "resource_id", name="uq_agent_resource_request"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    resource_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("file_resources.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=UTCNow())
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
