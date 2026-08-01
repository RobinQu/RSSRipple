"""Patched pyturso SQLAlchemy dialect registration.

Separate module from ``app.database`` so the SQLAlchemy dialect registry can
resolve the class path without a circular import (the registry loads lazily at
engine-creation time, which happens while ``app.database`` is initializing).

Two patches on top of pyturso's stock ``AioTursoDialect``:

1. ``has_stop`` — ``SQLiteDialect_aiosqlite.__init__`` probes this aiosqlite-
   specific attribute; pyturso's adapter doesn't define it.
2. ``supports_statement_cache`` — opt into SQL compilation caching, same as
   the aiosqlite dialect.
"""

from sqlalchemy.dialects import registry
from turso.sqlalchemy.dialect import AioTursoDialect, AsyncAdapt_turso_dbapi


class _PatchedTursoDbapi(AsyncAdapt_turso_dbapi):
    has_stop = False


class CompatAioTursoDialect(AioTursoDialect):
    supports_statement_cache = True

    @classmethod
    def import_dbapi(cls):
        import turso
        import turso.aio

        return _PatchedTursoDbapi(turso.aio, turso)


def register() -> None:
    """Register the patched dialect for ``sqlite+aioturso://`` URLs."""
    registry.register("sqlite.aioturso", "app.db_turso_dialect", "CompatAioTursoDialect")
