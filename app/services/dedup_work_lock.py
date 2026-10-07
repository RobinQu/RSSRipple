"""Protect work metadata and references throughout one merge transaction."""
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, OperationalError

from app.models.movie import Movie
from app.services.decision_rekey import lock_work_choice_agents
from app.services.work_deletion import lock_work_references


async def lock_merge_works(db, rows):
    # Existing transaction-local edits must reach storage before populate_existing.
    await db.flush()
    ordered = sorted(rows, key=lambda row: (row.__tablename__, row.id))
    groups = [("movie" if isinstance(row, Movie) else "series", [row.id]) for row in ordered]
    await lock_work_choice_agents(db, groups)
    postgres = db.get_bind().dialect.name == "postgresql"
    try:
        for row in ordered:
            model = type(row)
            if not postgres:
                # A stale Turso reader must collide with a committed editor before
                # deleting that editor's work. Keep updated_at unchanged.
                await db.execute(update(model).where(model.id == row.id).values(
                    id=model.id, updated_at=model.updated_at,
                ).execution_options(synchronize_session=False))
            current = await db.scalar(select(model).where(model.id == row.id)
                                      .with_for_update(nowait=True)
                                      .execution_options(populate_existing=True))
            if current is None:
                raise OperationalError("dedup changed target", {}, RuntimeError(
                    "database is locked: work changed; retry whole transaction",
                ))
            await lock_work_references(db, "movie" if isinstance(row, Movie) else "series", row.id)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        # Retry boundaries must discard this now-aborted transaction first.
        raise OperationalError("dedup lock unavailable", {}, RuntimeError(
            "database is locked: work is being edited; retry whole transaction",
        )) from exc
