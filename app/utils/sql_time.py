"""Database-generated timestamps matching the application's naive UTC storage."""
from sqlalchemy import DateTime
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement


def utc_now_sql(dialect: str) -> str:
    if dialect == 'postgresql':
        return "(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')"
    if dialect in {'sqlite', 'default'}:
        return 'CURRENT_TIMESTAMP'
    raise ValueError('Unsupported timestamp backend: ' + dialect)


class UTCNow(FunctionElement):
    type = DateTime()
    inherit_cache = True


@compiles(UTCNow)
def _compile_utc_timestamp(element, compiler, **kwargs):
    return utc_now_sql(compiler.dialect.name)
