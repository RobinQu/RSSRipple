"""Upgrade owned PostgreSQL defaults without guessing historical time zones."""
import re

from sqlalchemy import text

from app.utils.sql_time import UTCNow, utc_now_sql


def _definition(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(r'\s+', '', value.lower()).replace('::text', '')
    while value.startswith('(') and value.endswith(')'):
        depth = 0
        for index, token in enumerate(value):
            depth += (token == '(') - (token == ')')
            if depth == 0:
                break
        if index != len(value) - 1:
            break
        value = value[1:-1]
    return value


async def ensure_utc_timestamp_defaults(conn, metadata) -> None:
    """Called inside startup's existing advisory-lock/DDL transaction.

    Only model-owned UTC defaults are altered. Neither existing row values nor
    caller timestamps are reinterpreted. A drifted schema requires review.
    """
    if conn.dialect.name != 'postgresql':
        return
    expected = {
        (table.name, column.name)
        for table in metadata.tables.values()
        for column in table.columns
        if column.server_default is not None
        and isinstance(column.server_default.arg, UTCNow)
    }
    if not expected:
        return
    rows = (await conn.execute(text('''
        SELECT n.nspname AS schema_name, c.relname AS table_name,
               a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS data_type,
               pg_get_expr(d.adbin, d.adrelid) AS default_expr
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
        WHERE n.nspname = current_schema() AND c.relname = ANY(CAST(:tables AS text[]))
          AND a.attnum > 0 AND NOT a.attisdropped
    '''), {'tables': sorted({table for table, _ in expected})})).mappings().all()
    catalog = {(row['table_name'], row['column_name']): row for row in rows}
    changes = {}
    utc = {"current_timestampattimezone'utc'", "timezone('utc',current_timestamp)"}
    for key in sorted(expected):
        row = catalog.get(key)
        if row is None or row['data_type'] != 'timestamp without time zone':
            raise ValueError('Unexpected UTC timestamp column: ' + '.'.join(key))
        definition = _definition(row['default_expr'])
        if definition in utc:
            continue
        if definition not in {None, 'now()', 'current_timestamp', 'transaction_timestamp()'}:
            raise ValueError('Unexpected UTC timestamp default: ' + '.'.join(key))
        changes.setdefault((row['schema_name'], key[0]), []).append(key[1])
    # Validate every owned column first. Group DDL per table to avoid acquiring
    # the same table lock for each of its timestamp defaults.
    quote = conn.dialect.identifier_preparer.quote
    for (schema, table), columns in sorted(changes.items()):
        alterations = ', '.join(
            f'ALTER COLUMN {quote(column)} SET DEFAULT {utc_now_sql("postgresql")}'
            for column in columns
        )
        await conn.execute(text(f'ALTER TABLE {quote(schema)}.{quote(table)} {alterations}'))
