"""Upgrade incompatible native FTS indexes without changing shadow rows."""

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


async def _probe_index(connection: AsyncConnection, table: str) -> None:
    # A real match query opens the index storage; catalog presence is insufficient.
    result = await connection.execute(text(
        f"SELECT entity_id FROM {table} "
        "WHERE fts_match(title_cn, title_en, original_title, aliases, :query) LIMIT 1"
    ), {"query": "rssripplecompatibilityprobe"})
    result.all()


async def upgrade_legacy_index(engine: AsyncEngine, table: str, create_sql: str) -> bool:
    """Rebuild only the known old-format index, atomically; propagate failures."""
    if table not in {"tv_series_fts", "movie_fts", "audio_work_fts"}:
        raise ValueError("Unknown FTS shadow table")
    try:
        async with engine.connect() as connection:
            await _probe_index(connection, table)
        return False
    except DatabaseError as exc:
        expected = (
            f"FTS index {table}_idx was created by an older version of Turso "
            "and its storage format is no longer supported;"
        )
        if not str(exc.orig).startswith(expected):
            raise

    # The failed probe's connection/transaction is closed before DDL starts.
    # On a crash or exception the old index must remain recoverable on retry.
    async with engine.begin() as connection:
        await connection.execute(text(f"DROP INDEX {table}_idx"))
        await connection.execute(text(create_sql))
        await _probe_index(connection, table)
    return True
