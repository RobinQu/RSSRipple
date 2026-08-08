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
pair is unique so upserts are idempotent.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class WorkCollection(Base):
    __tablename__ = "work_collections"
    __table_args__ = (
        UniqueConstraint(
            "external_source", "external_id",
            name="uq_work_collections_source_external",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title_cn: Mapped[str] = mapped_column(String(512), nullable=False)
    title_en: Mapped[str | None] = mapped_column(String(512), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    external_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Remote TMDB image URL (no local caching in phase 1).
    poster_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # TMDB collection details don't carry an overview on the movie endpoint;
    # stays NULL until a user edits it.
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    series = relationship("TVSeries", back_populates="collection")
    movies = relationship("Movie", back_populates="collection")

    @property
    def display_name(self) -> str:
        """Display name used by the Filter DSL and API summaries."""
        return self.title_cn or self.title_en or ""
