"""Revocable execution ownership, separate from the run's history row."""

import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentRunLease(Base):
    __tablename__ = "agent_run_leases"
    __table_args__ = (Index("ix_agent_run_leases_expiry", "expires_at_epoch", "id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    token: Mapped[str] = mapped_column(String(36), nullable=False)
    # Internal deadline in whole UTC epoch seconds, evaluated by the database.
    expires_at_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
