"""Learned subtitle-group parsing mappings.

The table doubles as a small cache for compound release labels.  It keeps the
raw label stable while allowing the parser to improve its canonical member
list when a constrained metadata judge has more context.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.db_types import json_column
from app.utils.sql_time import UTCNow


class SubtitleGroupMapping(Base):
    __tablename__ = "subtitle_group_mappings"
    __table_args__ = (
        UniqueConstraint("normalized_key", name="uq_subtitle_group_mapping_key"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    raw_value: Mapped[str] = mapped_column(String(512), nullable=False)
    # The UniqueConstraint above already backs lookups; a separate single-column
    # index would be redundant.
    normalized_key: Mapped[str] = mapped_column(String(512), nullable=False)
    groups: Mapped[list[str]] = mapped_column(json_column(), nullable=False, default=list)
    # single/heuristic/llm/manual/unresolved
    resolution: Mapped[str] = mapped_column(String(16), nullable=False, default="unresolved")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), onupdate=UTCNow(), nullable=False
    )
