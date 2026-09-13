"""Retry state for tasks whose notification snapshot could not be built."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class NotificationBuildFailure(Base):
    __tablename__ = "notification_build_failures"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    download_task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("download_tasks.id", ondelete="CASCADE"), unique=True, nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    error_message: Mapped[str] = mapped_column(String(2048), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
