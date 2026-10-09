"""SchemaMigration ORM model — ledger of applied light-migration blocks.

``app.database._apply_light_migrations`` is a set of idempotent, probe-based
schema/data convergence blocks rather than a versioned migration tool. This
table records which named blocks have run to completion at least once
(``name`` = the block's label, ``applied_at`` = first completion, UTC).

The ledger is **observational, never the correctness authority**: every block
still probes the live schema/data on each startup and self-heals, so a ledger
row that disagrees with reality (e.g. a restored backup missing a column the
ledger claims) is harmless — the probe re-applies the change. There is no
down path; the ledger only makes the applied set inspectable and auditable.

The model is intentionally not re-exported from ``app.models.__init__``;
``create_tables`` imports this module before ``create_all`` so fresh and
upgraded databases both get the table.
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.sql_time import UTCNow


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=UTCNow(), nullable=False
    )
