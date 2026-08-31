"""Learned subtitle-group parsing mappings.

The table doubles as a small cache for compound release labels.  It keeps the
raw label stable while allowing the parser to improve its canonical member
list when a constrained metadata judge has more context.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SubtitleGroupMapping(Base):
    __tablename__ = "subtitle_group_mappings"
    __table_args__ = (
        UniqueConstraint("normalized_key", name="uq_subtitle_group_mapping_key"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    raw_value: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    groups: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # single/heuristic/llm/manual/unresolved
    resolution: Mapped[str] = mapped_column(String(16), nullable=False, default="unresolved")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
