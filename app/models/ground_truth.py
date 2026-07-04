"""GroundTruth entry — persisted labeled metadata for agent evaluation.

Each entry stores the raw RSS title, the human-verified ground truth,
and optionally the agent's original output for comparison.
Entries are grouped into named datasets (e.g., "v1", "v2").
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, JSON, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GroundTruthEntry(Base):
    __tablename__ = "ground_truth_entries"
    __table_args__ = (
        Index("ix_gt_dataset_name", "dataset_name"),
        Index("ix_gt_raw_title", "raw_title"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    dataset_name: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True,
        comment="Named dataset group, e.g. 'v1', 'v2'"
    )
    raw_title: Mapped[str] = mapped_column(
        String(1024), nullable=False,
        comment="Original RSS entry title (unique per dataset)"
    )
    source_feed: Mapped[str] = mapped_column(
        String(50), nullable=False, default="",
        comment="Source feed name: mikanani, kisssub, eztv, dmhy"
    )

    # ── Ground truth (human-verified) ──
    ground_truth_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict,
        comment="Full ResourceMetadata as JSON: clean_title, content_type, "
                "episode, season, matched_entity, etc."
    )

    # ── Agent result (for comparison / diff analysis) ──
    agent_result_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True,
        comment="Original agent output as JSON for comparison with ground truth"
    )

    # ── Review metadata ──
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending",
        comment="pending | draft | accepted | skipped"
    )
    reviewer_notes: Mapped[str | None] = mapped_column(
        String(2000), nullable=True,
        comment="Optional notes from the reviewer"
    )

    # ── Timestamps ──
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
