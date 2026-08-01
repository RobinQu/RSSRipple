"""Migrate an existing SQLite (aiosqlite) database file to a Turso database file.

Turso reads the SQLite file format as-is, so migration is a consistent file
copy plus two transformations:

1. **Drop FTS5 objects** — Turso does not implement the ``fts5`` virtual table
   module, and switching a database containing FTS5 tables to MVCC mode
   corrupts the connection's schema view. Full-text search falls back to LIKE
   matching on Turso (see ``app/services/fts.py``).
2. **Enable MVCC** — ``PRAGMA journal_mode='mvcc'`` is persistent per file and
   unlocks ``BEGIN CONCURRENT`` multi-writer transactions.

Usage::

    uv run python scripts/migrate_to_turso.py \
        --source data/rss_ripple_dev.db \
        --target data/rss_ripple_turso.db

After migration, point the app at the new file::

    DATABASE_URL=sqlite+aioturso:///data/rss_ripple_turso.db

The source file is left untouched. Re-running with the same target is refused
unless ``--force`` is given (the target is rebuilt from the source).
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

_FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")


def _copy_and_strip_fts5(source: Path, target: Path) -> list[str]:
    """Consistent-copy source → target via the backup API; drop FTS5 objects.

    Returns the list of dropped object names.
    """
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.execute("PRAGMA busy_timeout = 30000")
    dst = sqlite3.connect(target)
    src.backup(dst)  # handles WAL consistently; no -wal sidecar needed
    src.close()

    virtual = [
        row[0]
        for row in dst.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%USING fts5%'"
        ).fetchall()
    ]
    dropped: list[str] = []
    for name in virtual:
        dst.execute(f'DROP TABLE IF EXISTS "{name}"')
        dropped.append(name)
    # Drop any leftover shadow tables (dropping the virtual table normally
    # removes them, but be thorough for hand-edited databases).
    shadows = [
        row[0]
        for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if any(row[0] == v + suffix for v in virtual for suffix in _FTS_SHADOW_SUFFIXES)
    ]
    for name in shadows:
        dst.execute(f'DROP TABLE IF EXISTS "{name}"')
        dropped.append(name)
    dst.commit()
    dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dst.close()
    return dropped


async def _enable_mvcc_and_verify(target: Path) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    import app.models  # noqa: F401 — model discovery for metadata

    # Importing app.database registers the patched aioturso dialect.
    from app.database import normalize_database_url

    engine = create_async_engine(normalize_database_url(f"sqlite+aioturso:///{target}"))
    async with engine.begin() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode='mvcc'"))).scalar()
        tables = (
            await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            )
        ).fetchall()
        print(f"journal_mode: {mode}")
        print(f"tables visible: {len(tables)}")
        for probe in ("agents", "channels", "file_resources", "download_tasks"):
            count = (
                await conn.execute(text(f'SELECT COUNT(*) FROM "{probe}"'))
            ).scalar()
            print(f"  {probe}: {count} rows")
    await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="existing SQLite database file")
    parser.add_argument("--target", required=True, help="new Turso database file to create")
    parser.add_argument("--force", action="store_true", help="overwrite an existing target")
    args = parser.parse_args()

    source = Path(args.source)
    target = Path(args.target)
    if not source.exists():
        print(f"error: source {source} does not exist", file=sys.stderr)
        return 1
    if source.resolve() == target.resolve():
        print("error: source and target must differ", file=sys.stderr)
        return 1
    if target.exists():
        if not args.force:
            print(f"error: target {target} exists (use --force to rebuild)", file=sys.stderr)
            return 1
        target.unlink()
        for suffix in ("-wal", "-shm", "-log"):
            sidecar = target.with_name(target.name + suffix)
            if sidecar.exists():
                sidecar.unlink()

    dropped = _copy_and_strip_fts5(source, target)
    print(f"copied {source} -> {target}")
    print(f"dropped {len(dropped)} FTS5 object(s): {', '.join(dropped) or 'none'}")

    asyncio.run(_enable_mvcc_and_verify(target))
    print("\nDone. Start the app with:")
    print(f"  DATABASE_URL=sqlite+aioturso:///{target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
