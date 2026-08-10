"""Metadata cache for title cleaning and agent results.

Stores cached results keyed by ``(title, source)`` where ``source`` namespaces
the cache by both type and data source:

- ``"metadata_agent:<source>"`` - Full metadata agent result for one external
  source (e.g. ``"metadata_agent:jina"``, ``"metadata_agent:exa"``), including
  clean_title, content_type, inferred episode/season, matched entity, and
  confidence. Namespacing by source keeps one source's results from being
  served for a channel configured with a different source.
- ``"llm_title"`` - Legacy title cleaning cache (pre-refactor, retained for
  reference).

The ``metadata_json`` column stores the complete result dict, whose shape
depends on the ``source`` value.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Bump this whenever the classification / judge / matching logic that
# PRODUCES cached verdicts changes (e.g. the tv-vs-movie classifier). Rows
# written by older generations are treated as cache misses and lazily
# deleted on read, so stale verdicts from superseded logic can never
# short-circuit the fixed code. Legacy rows (pre-versioning) migrated to
# ``generation = 0`` and are therefore always stale.
# Generation history:
#   1 — initial versioning (tv-vs-movie classifier fix).
#   2 — P1 (Exa-fallback identity semantics) + P2 (wikipedia seasons/episodes
#       attach) + P3 (identity bag: alt_external_ids / langlink pageids)
#       changed verdict logic; stale cached verdicts must refetch.
#   3 — genre unification: the closed TMDB genre set is injected into the
#       judge/ReAct prompts and clamped via genre_registry.normalize_genres;
#       cached verdicts from older generations carry un-normalized genre.
#   4 — genre prompt becomes best-effort: the judge/ReAct instruction now
#       requires inferring at least one genre from the synopsis when the
#       source lists none, so genre-less verdicts should not reappear.
METADATA_CACHE_GENERATION = 4


class MetadataCache(Base):
    __tablename__ = "metadata_cache"
    __table_args__ = (
        UniqueConstraint("title", "source", name="uq_metadata_cache_key"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    content_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Logic generation that produced this verdict; see METADATA_CACHE_GENERATION.
    generation: Mapped[int] = mapped_column(
        nullable=False, default=METADATA_CACHE_GENERATION
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
