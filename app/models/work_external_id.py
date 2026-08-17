"""WorkExternalId — the "identity bag" reverse-mapping external ids to works.

A work (TVSeries/Movie) can carry MANY external identities discovered over
time: the wikipedia pageid it was created from, langlink pageids of the same
page in other language wikis, a tmdb/bangumi/... id found later by the Exa
fallback, etc. This table maps any known ``(source, external_id)`` pair back
to the work, making cross-source / cross-language upsert convergence
deterministic instead of title-luck.

Storage convention (mirrors ``TVSeries.external_id``): ``source`` holds the
registry source name (``wikipedia``/``tmdb``/...) and ``external_id`` holds
the FULL canonical ``source:id`` string (e.g. ``wikipedia:zh:7727654``,
``tmdb:82684``) as produced by
:func:`app.services.metadata_source_registry.canonicalize_external_id`.
Wikipedia pageids are per-language-edition, so the canonical wikipedia form
carries the edition (``wikipedia:{lang}:{pageid}``); legacy rows store the
bare ``wikipedia:{pageid}`` and both forms are matched on lookup
(``wikipedia_match_keys``).

Primary-id rule (creator-wins): the bag never replaces
``TVSeries.external_id``/``external_source`` (and the Movie equivalents) —
the primary display id stays whatever was set at row creation; later ids only
enter the bag. ``UniqueConstraint(source, external_id)`` guarantees one id
maps to at most one work; a conflicting add is logged (dedup candidate), not
stolen.

``work_id`` is deliberately FK-less: it points into either ``tv_series.id``
or ``movies.id`` depending on ``work_type`` (cross-table reference).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WorkExternalId(Base):
    __tablename__ = "work_external_ids"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_work_external_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # "series" | "movie" — which work table work_id points into.
    work_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # FK-less cross-table reference (tv_series.id or movies.id).
    work_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Registry source name (wikipedia/tmdb/bangumi/mal/anilist/imdb/douban).
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # Full canonical "source:id" string (mirrors TVSeries.external_id).
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
