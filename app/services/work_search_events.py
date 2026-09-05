"""ORM event hooks keeping the search indexes in sync with work rows.

Two backends, one pair of flush hooks that inspects the session's
new/dirty/deleted work objects (TVSeries / Movie / AudioWork /
WorkCollection):

* **Turso** — enqueue ``fts_outbox`` rows (``upsert``/``delete``) in the
  *same transaction* as the base row. The ``_drain_fts_outbox`` scheduler job
  replays them onto the FTS sidecar shadow tables, so sidecar writes are
  eventually consistent with the main database and never depend on a caller
  remembering to call ``upsert_*_fts``/``delete_*_fts`` (the old scattered
  call sites were removed). The sidecar covers the work tables only —
  WorkCollection changes never enter the outbox.

* **PostgreSQL** — recompute the ``search_text`` column (normalized
  concatenation of every title field) so it stays current for the ``pg_trgm``
  GIN indexes. There is no sidecar on PostgreSQL.

Both branches run on both backends harmlessly: ``search_text`` is maintained
on Turso too (uniform schema, single normalization point) and ``fts_outbox``
simply stays empty on PostgreSQL.

The outbox enqueue must happen in ``after_flush`` rather than ``before_flush``
because Python-side PK defaults (``default=lambda: uuid4()``) are only applied
during the flush — in ``before_flush`` a freshly-added object still has
``id is None``. ``search_text`` (PG) is set in ``before_flush`` so it is
persisted by the current flush. ``before_flush`` records the affected work
objects into ``session.info``; ``after_flush`` consumes them and enqueues,
de-duplicating per transaction (cleared on commit/rollback) so repeated
autoflush cycles only enqueue once. Because shadow writes are full-state
DELETE+INSERT, replaying an extra row is always idempotent.
"""

import logging

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.config import settings
from app.database import is_turso_url
from app.models.audio_work import AudioWork
from app.models.fts_outbox import FtsOutbox
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection

logger = logging.getLogger(__name__)

_SEARCH_SYNC_PENDING = "_search_sync_pending"
_SEARCH_SYNC_DONE = "_search_sync_done"

_SearchTextT = TVSeries | Movie | AudioWork | WorkCollection


def build_search_text(obj: _SearchTextT) -> str:
    """Normalized search haystack for a work/collection row.

    Concatenation of ``title_cn``/``title_en``/``original_title`` and every
    alias through ``normalize_title`` (NFKC + OpenCC t2s + lowercase).
    WorkCollection has no ``original_title`` column; everything else shares
    this exact representation, so matching semantics stay identical across
    backends and row kinds.
    """
    parts = [
        obj.title_cn,
        obj.title_en,
        getattr(obj, "original_title", None),
        *(obj.aliases or []),
    ]
    from app.services.text_normalizer import normalize_title

    return " ".join(n for n in (normalize_title(t) for t in parts) if n)


def _entity_type(obj: object) -> str | None:
    if isinstance(obj, TVSeries):
        return "series"
    if isinstance(obj, Movie):
        return "movie"
    if isinstance(obj, AudioWork):
        return "audio"
    return None


def _set_search_text(obj: _SearchTextT) -> None:
    text = build_search_text(obj)
    if getattr(obj, "search_text", None) != text:
        obj.search_text = text


def _enqueue(session: Session, done: set, etype: str, entity_id: str, op: str) -> None:
    key = (etype, entity_id, op)
    if key in done:
        return
    done.add(key)
    session.add(FtsOutbox(entity_type=etype, entity_id=entity_id, op=op))


@event.listens_for(Session, "before_flush")
def _before_flush(session, flush_context, instances) -> None:
    pending = session.info.setdefault(_SEARCH_SYNC_PENDING, set())
    turso = is_turso_url(settings.database_url)

    for obj in session.new:
        if isinstance(obj, WorkCollection):
            # search_text only — collections never enter the FTS outbox.
            _set_search_text(obj)
            continue
        etype = _entity_type(obj)
        if etype is None:
            continue
        _set_search_text(obj)
        if turso:
            pending.add(("upsert", obj))
    for obj in session.dirty:
        if isinstance(obj, WorkCollection):
            _set_search_text(obj)
            continue
        etype = _entity_type(obj)
        if etype is None:
            continue
        _set_search_text(obj)
        if turso:
            pending.add(("upsert", obj))
    if turso:
        for obj in session.deleted:
            etype = _entity_type(obj)
            if etype is not None:
                pending.add(("delete", obj))


@event.listens_for(Session, "after_flush")
def _after_flush(session, flush_context) -> None:
    pending = session.info.pop(_SEARCH_SYNC_PENDING, None)
    if not pending or not is_turso_url(settings.database_url):
        return
    done = session.info.setdefault(_SEARCH_SYNC_DONE, set())
    for op, obj in pending:
        etype = _entity_type(obj)
        if etype is None or obj.id is None:
            continue
        _enqueue(session, done, etype, obj.id, op)


@event.listens_for(Session, "after_commit")
def _after_commit(session) -> None:
    session.info.pop(_SEARCH_SYNC_PENDING, None)
    session.info.pop(_SEARCH_SYNC_DONE, None)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session) -> None:
    session.info.pop(_SEARCH_SYNC_PENDING, None)
    session.info.pop(_SEARCH_SYNC_DONE, None)
