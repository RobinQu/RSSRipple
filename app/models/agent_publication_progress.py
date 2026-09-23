"""Prototype state separating historical admission from event consumption."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentPublicationProgress(Base):
    __tablename__ = "agent_publication_progress"
    __table_args__ = (CheckConstraint("baseline >= 0 AND cursor >= 0", name="ck_agent_publication_progress"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), unique=True)
    channel_id: Mapped[str] = mapped_column(String(36), ForeignKey("channels.id", ondelete="CASCADE"))
    generation: Mapped[str] = mapped_column(String(36), nullable=False)
    baseline: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cursor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    historical_created_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
