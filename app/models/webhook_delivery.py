"""WebhookDelivery ORM model.

One row per (notification, webhook) pair: the fan-out unit of the download
notification pipeline. Each delivery carries its own status and retry
bookkeeping so one failing webhook never blocks the others. See
docs/design/notifications.md.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # Fan-out is idempotent: a notification produces at most one delivery
        # per webhook, no matter how often the fan-out pass runs.
        UniqueConstraint("notification_id", "webhook_id", name="uq_delivery_pair"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    notification_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("download_notifications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    webhook_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_webhooks.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Enum("pending", "done", "failed", name="delivery_status"),
        default="pending",
        nullable=False,
    )
    # Delivery bookkeeping: attempts so far and when the next one is due
    # (exponential backoff). ``next_attempt_at`` NULL on a pending row means
    # "due immediately".
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    notification = relationship("DownloadNotification", back_populates="deliveries")
    webhook = relationship("AgentWebhook", back_populates="deliveries")
