"""ApiKey ORM model — hashed API keys for programmatic access.

Only a SHA-256 hex digest of the key is stored; the plaintext is returned
exactly once (at creation). ``prefix`` keeps the first characters for display
so users can tell keys apart without exposing the secret.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.sql_time import UTCNow


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # First ~10 chars of the plaintext key — display only, never used to match.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # SHA-256 hex digest of the full plaintext key.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), nullable=False
    )
    # Naive UTC expiry (project time convention); NULL = never expires.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
