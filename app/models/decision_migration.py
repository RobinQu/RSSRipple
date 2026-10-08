"""Durable before-image for explicitly reviewed pending-decision migrations."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.sql_time import UTCNow


class DecisionMigration(Base):
    __tablename__ = "decision_migrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    review_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    original_review: Mapped[dict] = mapped_column(JSON, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=UTCNow(), nullable=False)
