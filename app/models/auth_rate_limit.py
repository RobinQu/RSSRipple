"""Persistent OTP attempt budgets shared by all web processes."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuthRateLimitBucket(Base):
    __tablename__ = "auth_rate_limit_buckets"
    __table_args__ = (CheckConstraint("attempts >= 1", name="ck_auth_budget_positive"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    bucket_key: Mapped[str] = mapped_column(String(72), unique=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    resets_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
