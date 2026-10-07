"""Durable metadata reparse intent, separate from manual confirmation dismissal."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ResourceReparseRequest(Base):
    __tablename__ = "resource_reparse_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    resource_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_resources.id", ondelete="CASCADE"), unique=True, nullable=False,
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
