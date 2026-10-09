"""WorkCollection ORM model — franchise grouping for related works.

A collection groups works of one IP (攻壳机动队, 蜘蛛侠, 狮子王 …) across the
TVSeries/Movie tables. It is an *organizational* layer, not the core of
disambiguation: matching/dispatch still key off the individual work rows.

One work belongs to at most one collection, enforced by the single nullable
``collection_id`` FK on TVSeries/Movie.

External identity uses ``external_source="tmdb_collection"`` + the raw TMDB
collection numeric id — deliberately NOT ``canonicalize_external_id`` (its
TMDB rule would rewrite ``tmdb-collection:131295`` to ``tmdb:131295`` and
collide with the movie id space). TV franchise grouping instead uses
``external_source="wikidata"`` + the franchise entity QID (see
``scripts/tv_collection_backfill.py``). The (external_source, external_id)
pair is unique *for identity-bearing rows only* (partial unique index
``WHERE external_id IS NOT NULL``) so upserts are idempotent, while shell
collections (``external_id NULL``) legitimately coexist in multiples.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.db_types import json_column
from app.utils.sql_time import UTCNow


class WorkCollection(Base):
    __tablename__ = "work_collections"
    __table_args__ = (
        # Identity-keyed upserts (tmdb_collection / wikidata) are unique on
        # (external_source, external_id), but ONLY for rows that carry an
        # external identity: shell collections (series_group, NULL id),
        # franchise packs and manual collections are legitimately plural, so
        # a plain UNIQUE constraint (NULLs distinct) would give no guarantee
        # while a COALESCE expression index would wrongly collapse them.
        # Partial unique index — same DDL on both backends.
        Index(
            "uq_work_collections_source_external",
            "external_source", "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title_cn: Mapped[str] = mapped_column(String(512), nullable=False)
    title_en: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Alternative titles (season-qualified variants, translations) used by the
    # two-level title fallback when matching series-level sources.
    aliases: Mapped[list | None] = mapped_column(json_column(), nullable=True)
    # Normalized search haystack (title_cn + title_en + aliases through
    # ``normalize_title``), maintained by the ORM before_flush hook — same
    # logic as the work tables, but collections are NOT mirrored into the
    # Turso FTS sidecar (works only).
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fields the user edited manually; automatic metadata scans skip these
    # (same contract as TVSeries/Movie.manually_edited_fields).
    manually_edited_fields: Mapped[list | None] = mapped_column(json_column(), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Remote TMDB image URL (no local caching in phase 1).
    poster_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # TMDB collection details don't carry an overview on the movie endpoint;
    # stays NULL until a user edits it.
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), onupdate=UTCNow(), nullable=False
    )

    # Relationships
    series = relationship("TVSeries", back_populates="collection")
    movies = relationship("Movie", back_populates="collection")

    @property
    def display_name(self) -> str:
        """Display name used by the Filter DSL and API summaries."""
        return self.title_cn or self.title_en or ""
