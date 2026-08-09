"""AgentWebhook ORM model.

Per-Agent webhook subscription for download notifications. An Agent may
register any number of webhooks; every notification fans out to one
``WebhookDelivery`` per enabled webhook. ``mock=True`` webhooks count
delivery as successful without any HTTP call, purely for inspecting
payloads. See docs/design/notifications.md.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AgentWebhook(Base):
    __tablename__ = "agent_webhooks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    mock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Disabled webhooks keep their row (and delivery history) but receive no
    # new deliveries; re-enabling resumes fan-out from the backlog.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    agent = relationship("Agent", back_populates="webhooks")
    deliveries = relationship(
        "WebhookDelivery",
        back_populates="webhook",
        passive_deletes=True,
    )
