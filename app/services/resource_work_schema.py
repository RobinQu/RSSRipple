"""Upgrade existing FileResource tables without guessing conflicting work IDs."""
import re

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.models.file_resource import RESOURCE_WORK_FK_CHECK, RESOURCE_WORK_FK_CONSTRAINT

_COLUMNS = {'series_id', 'movie_id', 'audio_work_id'}


def _normalized(sql: str) -> str:
    return re.sub(r'\s+', '', sql.lower()).replace('ifnotexists', '')


def _unwrap(sql: str) -> str:
    """Remove only parentheses enclosing the entire expression."""
    while sql.startswith('(') and sql.endswith(')'):
        depth = 0
        for index, token in enumerate(sql):
            depth += (token == '(') - (token == ')')
            if depth == 0:
                break
        if index != len(sql) - 1:
            break
        sql = sql[1:-1]
    return sql


def _check_signature(sql: str) -> str | None:
    # PostgreSQL adds parentheses around associative sums. Ignore those only
    # when <= remains the outer operator, never A + (B + C <= 1).
    sql = _unwrap(_normalized(sql).replace('"', ''))
    parts = sql.split('<=')
    if len(parts) != 2 or _unwrap(parts[1]) != '1':
        return None
    depth = 0
    for token in parts[0]:
        depth += (token == '(') - (token == ')')
        if depth < 0:
            return None
    if depth:
        return None
    return parts[0].replace('(', '').replace(')', '')


def _trigger_sql(operation: str) -> str:
    expression = re.sub(r'\b(series_id|movie_id|audio_work_id)\b', r'NEW.\1', RESOURCE_WORK_FK_CHECK)
    return (
        f'CREATE TRIGGER IF NOT EXISTS {RESOURCE_WORK_FK_CONSTRAINT}_{operation.lower()} '
        f'BEFORE {operation} ON file_resources WHEN NOT ({expression}) '
        f"BEGIN SELECT RAISE(ABORT, '{RESOURCE_WORK_FK_CONSTRAINT}'); END"
    )


async def ensure_resource_work_fk_guard(conn: AsyncConnection) -> None:
    """Validate existing rows and atomically install owned write guards.

    The caller owns the transaction. Invalid history and changed definitions
    fail migration; no reference is silently discarded to make DDL succeed.
    """
    backend = conn.dialect.name
    if backend not in {'sqlite', 'postgresql'}:
        raise ValueError('Unsupported resource work FK backend')
    if backend == 'postgresql':
        # Close the gap between inspecting old rows and installing protection.
        # Schema migration is an offline operation; release at caller commit.
        await conn.execute(text('LOCK TABLE file_resources IN ACCESS EXCLUSIVE MODE'))
    columns = await conn.run_sync(lambda sync: {c['name'] for c in inspect(sync).get_columns('file_resources')})
    missing = sorted(_COLUMNS - columns)
    if missing:
        raise ValueError('Missing required resource work FK columns: ' + ', '.join(missing))
    constraints = await conn.run_sync(lambda sync: inspect(sync).get_check_constraints('file_resources'))
    present = False
    for constraint in constraints:
        if constraint['name'] == RESOURCE_WORK_FK_CONSTRAINT:
            if _check_signature(constraint['sqltext']) != _check_signature(RESOURCE_WORK_FK_CHECK):
                raise ValueError('Unexpected resource work FK CHECK definition')
            present = True
    if backend == 'sqlite':
        for operation in ('INSERT', 'UPDATE'):
            existing = await conn.scalar(text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"
            ), {'name': f'{RESOURCE_WORK_FK_CONSTRAINT}_{operation.lower()}'})
            if existing and _normalized(existing) != _normalized(_trigger_sql(operation)):
                raise ValueError('Unexpected resource work FK trigger definition')
    invalid = list((await conn.execute(text(
        f'SELECT id FROM file_resources WHERE NOT ({RESOURCE_WORK_FK_CHECK}) ORDER BY id LIMIT 20'
    ))).scalars())
    if invalid:
        raise ValueError('Resource work FKs require reviewed migration; sample IDs: ' + ', '.join(invalid))
    if backend == 'postgresql':
        if not present:
            await conn.execute(text(
                f'ALTER TABLE file_resources ADD CONSTRAINT {RESOURCE_WORK_FK_CONSTRAINT} '
                f'CHECK ({RESOURCE_WORK_FK_CHECK})'
            ))
        # A matching NOT VALID constraint is not proof that history is valid.
        await conn.execute(text(
            f'ALTER TABLE file_resources VALIDATE CONSTRAINT {RESOURCE_WORK_FK_CONSTRAINT}'
        ))
    elif not present:
        for operation in ('INSERT', 'UPDATE'):
            await conn.execute(text(_trigger_sql(operation)))


async def upgrade_sqlite_resource_work_fk(engine: AsyncEngine) -> None:
    """Own ordinary startup DDL transaction after legacy columns are added."""
    async with engine.begin() as conn:
        await conn.execute(text("BEGIN"))
        await ensure_resource_work_fk_guard(conn)
