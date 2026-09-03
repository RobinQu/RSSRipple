"""FileResource ORM model."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class FileResource(Base):
    __tablename__ = "file_resources"
    __table_args__ = (
        UniqueConstraint("channel_id", "guid", name="uq_file_resources_channel_guid"),
        Index(
            "ix_file_resources_confirmation_created",
            "confirmation_ignored_at", "created_at", "id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    channel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    guid: Mapped[str] = mapped_column(String(512), nullable=False)
    title_raw: Mapped[str] = mapped_column(String(1024), nullable=False)
    title_cn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title_en: Mapped[str | None] = mapped_column(String(512), nullable=True)
    search_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    subtitle_group: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Canonical release-group representation.  ``subtitle_group`` is retained
    # as a legacy/raw compatibility column during the migration window.
    subtitle_groups: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    subtitle_groups_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Release year parsed from the raw title ("[2026]" / standalone token).
    # Used by the Layer-3 local-match year guard (same-title remakes).
    title_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ── Multi-episode batch (合集) support ──
    # ``is_batch`` marks a torrent that contains multiple episodes (S01E01~13,
    # [01-12 合集], "Season Pack", 全集 …). Batch resources dedup/conflict by
    # *content coverage* (see ``batch_seasons`` below and
    # ``agent_service._batch_coverage_key``): two batches are duplicates only
    # when they cover exactly the same content — same-coverage versions go
    # through the normal ask/auto conflict resolution, different coverage
    # (S1 pack vs S2 pack) never conflicts. Users can still pre-filter via
    # the ``is_batch`` DSL field.
    # ``episode_start`` / ``episode_end`` are best-effort — the raw title may
    # omit the boundaries (e.g. "Batch", "Full Season").
    is_batch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    episode_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Batch scope sub-classification (torrent content detection P1):
    #   NULL           – not a batch (single-episode resource).
    #   "season"       – single-season pack (all files in one season).
    #   "multi_season" – pack spanning multiple seasons of one work.
    #   "franchise"    – pack spanning multiple works (linked via
    #                    ``collection_id`` below).
    batch_scope: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Seasons covered by a multi_season/franchise pack (JSON int list),
    # persisted from the torrent content analysis. Drives the strict
    # content-coverage dedup of batch resources in the agent runner.
    # NULL = coverage unknown (title-only packs) → no cross-version dedup.
    batch_seasons: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    # Per-season episode ranges of a batch resource
    # ([{season, episode_start, episode_end}, ...]) from the torrent content
    # analysis / the edit wizard. Recomputed from file assignments whenever
    # those change; kept denormalized for cheap dedup/organize reads.
    season_ranges: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # ── Cross-season episode reconciliation ──
    # RSS titles sometimes number episodes absolutely across all seasons
    # (e.g. ``S04 - 84`` where 84 = cumulative count across seasons 1-4)
    # rather than per-season. When the MetadataAgent recognizes this via
    # TMDB/web-fallback ``seasons: [{season_number, episode_count}]`` evidence, it
    # rewrites ``episode`` to the per-season number and preserves the
    # original in ``absolute_episode`` for audit.
    # ``episode_confidence`` records where the value came from:
    #   "raw"          – untouched (title was already per-season, or no
    #                    evidence available).
    #   "reconciled"   – converted from absolute → per-season by the agent.
    #   "ambiguous"    – agent has evidence but couldn't converge; resource
    #                    is routed to Channel resource confirmation.
    #   "manual"       – user corrected via ``PATCH /resources/{id}/episode``.
    #   None           – legacy row created before this column existed.
    absolute_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(100), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subtitle_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # BCP-47 language tags detected on the raw title (best-effort). Sentinel
    # ``["multi"]`` marks titles that only say "multi-language" without
    # spelling out which ones. ``None`` = never populated (legacy row);
    # ``[]`` = parsed but no explicit marking.
    subtitle_langs: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    container: Mapped[str | None] = mapped_column(String(20), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    torrent_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # Local relative path of the downloaded-and-cached .torrent file (torrent
    # content detection P1). The bytes are cached on disk only — never stored
    # in the DB. NULL = not (yet) fetched.
    torrent_file: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    detail_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Linked entities — set after metadata resolution
    series_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tv_series.id", ondelete="SET NULL"), nullable=True
    )
    movie_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("movies.id", ondelete="SET NULL"), nullable=True
    )
    # Non-TV/non-movie work (ASMR / music / drama CD / radio). Resolved via
    # general-purpose sources (Wikipedia / web fallback) only.
    audio_work_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("audio_works.id", ondelete="SET NULL"), nullable=True
    )
    # Franchise-pack link (WorkCollection): only set when ``batch_scope ==
    # "franchise"``. FK-exclusivity invariant extended: when this is non-null,
    # ``series_id`` / ``movie_id`` / ``audio_work_id`` must all be NULL (same
    # convention-only discipline as the existing work-FK exclusivity — no
    # CheckConstraint).
    collection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("work_collections.id"), nullable=True
    )
    metadata_matched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ── Metadata retry state ──
    # ``metadata_matched_at`` only records *successful* links, so a failed
    # attempt is indistinguishable from "never tried". These three columns
    # close that gap and drive the fetch-time backfill of unmatched resources:
    #   * ``metadata_attempts`` — how many times the metadata pipeline ran.
    #   * ``last_metadata_attempt_at`` — when it last ran (for backoff / TTL).
    #   * ``metadata_failure_type`` — None on success or "never tried";
    #     "transient" (timeout/connection/LLM-format → retry with backoff),
    #     "not_found" (source had no match → retry after a long TTL),
    #     "non_work" (correctly identified as music/ASMR/OP → never retry).
    metadata_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_metadata_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_failure_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # User explicitly dismissed this resource from Channel metadata
    # confirmations.  The resource remains available for search/audit, but is
    # no longer surfaced as a dashboard todo.
    confirmation_ignored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    channel = relationship("Channel", back_populates="file_resources")
    series = relationship("TVSeries", back_populates="file_resources")
    movie = relationship("Movie", back_populates="file_resources")
    audio_work = relationship("AudioWork", back_populates="file_resources")
    collection = relationship("WorkCollection")
    # Multi-work associations and per-main-file work/season/episode mappings.
    # Assignments cover single TV/movie releases as well as batch packs so
    # notification/organize never needs a second file-identity model.
    work_links = relationship(
        "ResourceWorkLink",
        back_populates="resource",
        cascade="all, delete-orphan",
        order_by="ResourceWorkLink.created_at.asc()",
    )
    file_assignments = relationship(
        "ResourceFileAssignment",
        back_populates="resource",
        cascade="all, delete-orphan",
        order_by="ResourceFileAssignment.file_path.asc()",
    )
    # delete-orphan is required: without it the ORM nullifies
    # download_tasks.file_resource_id when a resource is deleted (e.g. via
    # channel cascade), which violates the NOT NULL constraint.
    download_tasks = relationship(
        "DownloadTask", back_populates="file_resource", cascade="all, delete-orphan"
    )
