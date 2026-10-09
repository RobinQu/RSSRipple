"""Shared SQLAlchemy column types for the dual-backend schema.

Turso/SQLite stores JSON as TEXT; PostgreSQL gets native JSONB so the
``create_all`` path matches the light-migration ``ADD COLUMN ... JSONB``
DDL (``app.database._apply_light_migrations``).
"""

from sqlalchemy import JSON
from sqlalchemy.dialects import postgresql


def json_column() -> JSON:
    """Generic JSON on Turso/SQLite, native JSONB on PostgreSQL."""
    return JSON().with_variant(postgresql.JSONB(), "postgresql")
