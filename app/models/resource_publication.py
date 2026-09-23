"""Transactional channel publication order; sequence allocation owns a row lock."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ChannelPublicationCounter(Base):
    __tablename__ = "channel_publication_counters"
    __table_args__ = (CheckConstraint("sequence >= 0", name="ck_publication_counter_nonnegative"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id: Mapped[str] = mapped_column(String(36), ForeignKey("channels.id", ondelete="CASCADE"), unique=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class ResourcePublication(Base):
    __tablename__ = "resource_publications"
    __table_args__ = (
        UniqueConstraint("channel_id", "sequence", name="uq_resource_publication_channel_sequence"),
        CheckConstraint(
            "sequence > 0 AND origin_sequence > 0 AND origin_sequence <= sequence", name="ck_publication_order"
        ),
        CheckConstraint("kind IN ('created', 'metadata')", name="ck_publication_kind"),
        UniqueConstraint("resource_id", "kind", name="uq_resource_publication_resource_kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id: Mapped[str] = mapped_column(String(36), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    resource_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_resources.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
