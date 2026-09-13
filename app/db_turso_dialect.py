"""Patched pyturso SQLAlchemy dialect registration.

Separate module from ``app.database`` so the SQLAlchemy dialect registry can
resolve the class path without a circular import (the registry loads lazily at
engine-creation time, which happens while ``app.database`` is initializing).

Compatibility patches on top of pyturso's stock ``AioTursoDialect``:

1. ``has_stop`` — ``SQLiteDialect_aiosqlite.__init__`` probes this aiosqlite-
   specific attribute; pyturso's adapter doesn't define it.
2. ``supports_statement_cache`` — opt into SQL compilation caching, same as
   the aiosqlite dialect.
3. Discard only the physical connection after a write conflict; Turso may
   retain a stale read snapshot after a failed SAVEPOINT and rollback.
"""

from sqlalchemy import event
from sqlalchemy.dialects import registry
from turso.sqlalchemy.dialect import AioTursoDialect, AsyncAdapt_turso_dbapi


class _PatchedTursoDbapi(AsyncAdapt_turso_dbapi):
    has_stop = False


class CompatAioTursoDialect(AioTursoDialect):
    supports_statement_cache = True

    def do_terminate(self, dbapi_connection):
        # The generic Turso adapter lacks aiosqlite terminate(). has_stop=False
        # disables GC termination; explicit invalidation can await close.
        dbapi_connection.close()

    @classmethod
    def import_dbapi(cls):
        import turso
        import turso.aio

        return _PatchedTursoDbapi(turso.aio, turso)


@event.listens_for(CompatAioTursoDialect, "handle_error")
def _discard_conflicted_connection(context):
    """A failed Turso SAVEPOINT can leave a stale read snapshot after rollback.

    Discard only that physical connection; keep the original DBAPI exception
    so existing transaction-level retry policies still decide what to replay.
    Never invalidate unrelated connections or turn arbitrary SQL errors into
    transient failures.
    """
    error = context.original_exception
    if not isinstance(error, context.dialect.dbapi.DatabaseError):
        return
    message = str(error).lower()
    if "database is locked" in message or "write-write conflict" in message:
        context.is_disconnect = True
        context.invalidate_pool_on_disconnect = False


def register() -> None:
    """Register the patched dialect for ``sqlite+aioturso://`` URLs."""
    registry.register("sqlite.aioturso", "app.db_turso_dialect", "CompatAioTursoDialect")
