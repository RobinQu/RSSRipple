"""TVSeries ORM model."""

import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TVSeries(Base):
    __tablename__ = "tv_series"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title_cn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title_en: Mapped[str | None] = mapped_column(String(512), nullable=True)
    original_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Normalized search haystack (title_cn + title_en + original_title +
    # aliases through ``normalize_title``), maintained by the ORM before_flush
    # hook. Indexed with pg_trgm GIN on PostgreSQL; Turso mirrors it into the
    # FTS sidecar via the fts_outbox drain.
    search_text: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    external_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    poster_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    genre: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    number_of_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    number_of_seasons: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-season episode counts as returned by TMDB/Exa
    # ([{season_number, episode_count}, ...]). Drives cross-season episode
    # reconciliation for resources that link to this series without going
    # through the metadata agent (known-work short-circuit / fuzzy auto-link).
    seasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Tri-state anime flag (orthogonal to content_type's medium): True =
    # Japanese-style animation, False = confirmed live-action, None = not yet
    # determined. Assigned from deterministic evidence (bangumi/mal/anilist
    # identity, Wikipedia TVAnime infobox, TMDB Animation+ja) with an LLM
    # fallback — see app/services/anime_signals.py.
    is_anime: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    canonical_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    wikipedia_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    wikipedia_page_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Franchise grouping (WorkCollection). At most one collection per work.
    collection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("work_collections.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    episodes = relationship(
        "Episode", back_populates="series", order_by="Episode.season.asc(), Episode.episode.asc()",
        cascade="all, delete-orphan",
    )
    file_resources = relationship("FileResource", back_populates="series")
    agent_works = relationship(
        "AgentWork", back_populates="series"
    )
    raw_title_mappings = relationship(
        "ChannelRawTitleMapping", back_populates="series"
    )
    pending_decisions = relationship(
        "PendingDecision", back_populates="series"
    )
    collection = relationship("WorkCollection", back_populates="series")
