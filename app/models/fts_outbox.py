"""FtsOutbox — durable change log driving Turso-sidecar FTS sync.

The FTS sidecar (``<main>_fts.db``, native ``USING fts`` ngram index) cannot
live inside the MVCC main database (custom index modules are unsupported in
MVCC mode), so its shadow tables are maintained by a background drain job.
This table is the *durable record* of work-row changes: rows are enqueued by
an ORM ``before_flush`` hook in the **same transaction** as the base row, so a
commit that persists a TVSeries/Movie/AudioWork change is guaranteed to leave
an outbox row behind (and a rollback removes it with the rest). The drain job
claims batches here and replays them against the sidecar.

No UNIQUE constraint on ``(entity_type, entity_id)``: repeated updates to the
same work between drain ticks just produce multiple rows — the shadow write is
an idempotent DELETE+INSERT, so replaying extras is harmless, and every row is
deleted from the outbox once drained. ``op`` is ``upsert`` | ``delete``.

PostgreSQL never writes to this table (its search path uses the in-table
``search_text`` column + ``pg_trgm`` GIN instead); the table still exists so
``create_all`` is backend-uniform, but stays empty.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class FtsOutbox(Base):
    __tablename__ = "fts_outbox"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # "series" | "movie" | "audio" — which work table entity_id points into.
    entity_type: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # "upsert" | "delete"
    op: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
