"""Channel ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.services.required_fields import normalize_required_fields


def _default_required_fields() -> list[str]:
    """Baseline required-fields list for new channels plus shape fields."""
    return normalize_required_fields([])


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(
        Enum("rss_feed", name="channel_type"), default="rss_feed", nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    fetch_interval: Mapped[int] = mapped_column(Integer, default=1800, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("active", "inactive", "error", name="channel_status"),
        default="active",
        nullable=False,
    )
    field_mapping: Mapped[dict] = mapped_column(JSON, nullable=False)
    metadata_agent_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # External metadata source used when metadata_agent_enabled is true.
    # Two-source architecture (Phase P1): "wikipedia" or "tmdb"; None falls
    # back to the default source (wikipedia) at runtime.
    metadata_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Ordered web-search fallback site whitelist (JSON list of registry source
    # names). None = default order; [] = fallback disabled. The fallback
    # supplies identity/links only - content follows the primary source.
    metadata_fallback_sources: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Channel-declared "required" metadata fields (JSON list of catalog
    # keys, see app/services/required_fields.py). Drives the resource-list
    # display column and the agent filter-DSL gating. Mandatory and add-only
    # after creation: defaults to the code-enforced baseline (locked six);
    # the startup light migration converges legacy NULL/partial rows to the
    # same baseline. There is no "unrestricted" state anymore.
    required_metadata_fields: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, default=lambda: _default_required_fields()
    )
    # "默认标记为 Anime": works linked from this channel's successfully parsed
    # resources get is_anime=True. Immutable after creation (update API 422s
    # on change attempts).
    default_is_anime: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
    # ── Auto-cleanup of stale unresolved resources ──
    # When enabled, the daily cleanup job deletes this channel's FileResources
    # that have sat *unresolved* (no linked work, never matched) for longer
    # than ``auto_cleanup_unresolved_days`` and have had no manual handling
    # (no direct download, no manual episode/metadata edit). Opt-in per channel.
    auto_cleanup_unresolved_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
    # Age (days) a resource may stay unresolved before auto-cleanup. 21 = 3 weeks.
    auto_cleanup_unresolved_days: Mapped[int] = mapped_column(
        Integer, default=21, nullable=False, server_default="21"
    )
    # ── Periodic work-metadata refresh (per-channel) ──
    # When enabled, a scheduler job periodically re-runs the shared
    # ``refresh_work_metadata`` pipeline (missing-fields-only fill, manual
    # edits never overridden) for the works linked to this channel's
    # resources, using this channel's own ``metadata_source``. NULL interval
    # → DEFAULT_METADATA_REFRESH_INTERVAL_MINUTES.
    metadata_refresh_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
    metadata_refresh_interval_minutes: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # Skip the missing-fields gate: refresh ALL linked works each run instead
    # of only those with fillable empty fields.
    metadata_refresh_full_scope: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_fetch_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_fetch_error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    file_resources = relationship(
        "FileResource", back_populates="channel", lazy="selectin", cascade="all, delete-orphan"
    )
    agents = relationship(
        "Agent", back_populates="channel", lazy="selectin", cascade="all, delete-orphan"
    )
    raw_title_mappings = relationship(
        "ChannelRawTitleMapping",
        back_populates="channel",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
