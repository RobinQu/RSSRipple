"""Keep the derived search text unbounded on new and upgraded databases."""
from sqlalchemy import text

_TABLES = ("audio_works", "movies", "tv_series", "work_collections")


async def ensure_search_text_columns(conn):
    """Widen owned PostgreSQL columns within the caller's startup transaction.

    Turso does not enforce VARCHAR length; its existing values need no rewrite.
    Validate every column before DDL so unexpected schema is never guessed.
    """
    if conn.dialect.name != "postgresql":
        return
    rows = (await conn.execute(text(
        "SELECT table_name, data_type, domain_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND column_name = 'search_text' "
        "AND table_name IN ('audio_works', 'movies', 'tv_series', 'work_collections')"
    ))).mappings().all()
    columns = {row["table_name"]: row for row in rows}
    for table in _TABLES:
        column = columns.get(table)
        if column is None or column["domain_name"] is not None or column["data_type"] not in {
            "text", "character varying",
        }:
            raise RuntimeError(f"Unexpected search text schema: {table}.search_text")
    for table in _TABLES:
        if columns[table]["data_type"] == "character varying":
            await conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN search_text TYPE TEXT'))
