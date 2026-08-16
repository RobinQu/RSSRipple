"""Migrate a Turso (embedded, SQLite-compatible) database to PostgreSQL.

RSSRipple's two database backends share one SQLAlchemy model layer, so
migration is a per-table copy driven by ``Base.metadata.sorted_tables`` (which
orders tables by foreign-key dependency). Rows are copied through SQLAlchemy
Core using the ORM's own table definitions, so dialect-specific column types
convert automatically on both ends:

- JSON stored as ``TEXT`` on Turso is deserialized by the source result
  processors and re-serialized to ``JSONB`` by the target bind processors.
- ``BOOLEAN`` 0/1 integers become ``true``/``false``.
- ``DATETIME`` strings are parsed and re-emitted as ``timestamp``.
- UUIDs, ``created_at``/``updated_at`` and every primary/foreign key are
  preserved verbatim (the schema is created first, so no ORM defaults fire).

The Turso-only ``fts_outbox`` change log is skipped: PostgreSQL keeps the
``search_text`` column in sync directly and never reads the outbox. The FTS
sidecar (``<main>_fts.db``) is also not migrated — PostgreSQL has no sidecar;
search runs against ``search_text`` + ``pg_trgm`` GIN indexes, which this
script creates on the target.

The source database must not be open by a running app (Turso holds a
single-process file lock). Stop the app first, or migrate a copied file.

Usage::

    uv run python scripts/migrate_to_postgres.py \
        --source sqlite+aioturso:///data/rss_ripple_turso.db \
        --target postgresql+asyncpg://rssripple:rssripple@localhost:5432/rssripple

After migration, point the app at the new database::

    DATABASE_URL=postgresql+asyncpg://rssripple:rssripple@localhost:5432/rssripple

The target must be empty (or ``--force`` given to drop-and-recreate its schema).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import Base, normalize_database_url  # noqa: E402

# Turso-only: the outbox change log is drained onto the FTS sidecar, which
# PostgreSQL does not have. Skipping it avoids copying stale pending rows.
_SKIP_TABLES = {"fts_outbox"}


async def _create_target_schema(engine, force: bool) -> None:
    """Create the full schema + pg_trgm search indexes on the target."""
    from app.database import _ensure_pg_trgm_indexes

    async with engine.begin() as conn:
        if force:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_pg_trgm_indexes(conn)


async def _copy_table(src_conn, dst_conn, table) -> tuple[int, int]:
    """Copy one table's rows from source to target. Returns (copied, skipped)."""
    name = table.name
    if name in _SKIP_TABLES:
        return 0, 0

    stmt = select(table)
    result = await src_conn.execute(stmt)
    rows = [dict(r._mapping) for r in result.all()]
    if not rows:
        return 0, 0

    await dst_conn.execute(table.insert(), rows)
    return len(rows), 0


async def _migrate(source_url: str, target_url: str, force: bool) -> None:
    import app.models  # noqa: F401 — model discovery for metadata

    src_engine = create_async_engine(normalize_database_url(source_url))
    dst_engine = create_async_engine(target_url)

    await _create_target_schema(dst_engine, force)

    # Table counts for the summary, captured before copying.
    tables = list(Base.metadata.sorted_tables)
    total = 0
    try:
        async with src_engine.connect() as src_conn, dst_engine.begin() as dst_conn:
            for table in tables:
                if table.name in _SKIP_TABLES:
                    print(f"  skip   {table.name} (Turso-only)")
                    continue
                n, _ = await _copy_table(src_conn, dst_conn, table)
                total += n
                if n:
                    print(f"  copied {table.name:32} {n} rows")
    finally:
        await src_engine.dispose()
        await dst_engine.dispose()

    # Recompute search_text for any rows whose normalized haystack is missing,
    # so the pg_trgm indexes are complete on first query.
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.services.fts import backfill_search_text

    factory = async_sessionmaker(dst_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        updated = await backfill_search_text(session)
        await session.commit()
        if updated:
            print(f"backfilled search_text on {updated} rows")

    print(f"\nDone. Migrated {total} rows into {target_url}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="Turso database URL (sqlite+aioturso://…)")
    parser.add_argument("--target", required=True, help="PostgreSQL database URL (postgresql+asyncpg://…)")
    parser.add_argument("--force", action="store_true", help="drop-and-recreate the target schema first")
    args = parser.parse_args()

    if "aioturso" not in args.source and "turso" not in args.source:
        print(f"error: --source must be a Turso URL, got {args.source!r}", file=sys.stderr)
        return 1
    if "postgresql" not in args.target:
        print(f"error: --target must be a PostgreSQL URL, got {args.target!r}", file=sys.stderr)
        return 1

    asyncio.run(_migrate(args.source, args.target, args.force))
    return 0


if __name__ == "__main__":
    sys.exit(main())
