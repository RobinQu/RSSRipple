"""EvalJob — persisted background agent job for the Metadata Eval Tool.

Stores the full job state (titles, partial results, status) so that
uncompleted jobs survive server restarts and can be automatically resumed.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.db_types import json_column
from app.utils.sql_time import UTCNow


class EvalJob(Base):
    __tablename__ = "eval_jobs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()),
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running",
        comment="running | completed | failed",
    )
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Full title objects [{id, raw_title, source_feed, ...}] — needed for resume
    titles: Mapped[list] = mapped_column(json_column(), nullable=False, default=list)
    # Just the title IDs — for quick status checks
    title_ids: Mapped[list] = mapped_column(json_column(), nullable=False, default=list)
    # Partial results {title_id: result_dict} — populated incrementally
    results: Mapped[dict] = mapped_column(json_column(), nullable=False, default=dict)

    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), onupdate=UTCNow(), nullable=False,
    )
