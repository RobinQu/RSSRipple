"""Immutable identity reservation for a queued download operation."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, false
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.sql_time import UTCNow


class DownloadDispatch(Base):
    __tablename__ = "download_dispatches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Reserved before a DownloadTask exists. Retained after its deletion so an
    # old execution cannot recreate a task that a user deliberately removed.
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    job_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    settled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=UTCNow(), index=True)
