"""DownloadNotification ORM model.

Download-task-driven notification queue. Each notification belongs to an
Agent (one FIFO queue per Agent, ordered by ``created_at``) and snapshots
everything an external consumer (e.g. vault-organizer) needs to post-process
a completed download: task, resource and work metadata, plus the torrent
file listing. Delivery fans out to the Agent's registered webhooks as
individual ``WebhookDelivery`` rows with per-webhook exponential backoff.
See docs/design/notifications.md.

Note: the physical table still carries the pre-fan-out delivery columns
(``status``, ``error_message``, ``attempt_count``, ``next_attempt_at``,
``notified_at``, ``processed_at``) as inert orphans — they were removed from
the ORM without a DROP migration.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DownloadNotification(Base):
    __tablename__ = "download_notifications"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # Queue ownership: every Agent has a singleton FIFO queue. Notifications
    # keep their agent reference even if the agent is later deleted (SET
    # NULL) so historical records stay inspectable.
    agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    # Unique: a download task produces at most one notification — this is
    # what makes both fresh enqueueing and manual backfill idempotent.
    download_task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("download_tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    agent = relationship("Agent", back_populates="notifications")
    download_task = relationship("DownloadTask", back_populates="notification")
    deliveries = relationship(
        "WebhookDelivery",
        back_populates="notification",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
