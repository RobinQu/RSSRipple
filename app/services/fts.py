"""Full-text search index management for TVSeries, Movie, and AudioWork.

Backed by Turso's native FTS (Tantivy, ``CREATE INDEX ... USING fts``) with
the ``ngram`` tokenizer for CJK-friendly substring matching.

**Sidecar architecture**: Turso's FTS indexes ("custom index modules") are
incompatible with the MVCC journal mode used by the main database, so the FTS
shadow tables live in a *separate* Turso database file (WAL mode) derived
from the main ``DATABASE_URL`` — e.g. ``rss_ripple_turso.db`` →
``rss_ripple_turso_fts.db``. The sidecar is a pure search index: it is
rebuildable from the base tables at any time and never holds authoritative
data.

Design:
- **Shadow tables** (``tv_series_fts`` / ``movie_fts`` / ``audio_work_fts``)
  holding *normalized* text — all indexed content is passed through
  ``text_normalizer.normalize_title`` (NFKC + OpenCC t2s + lowercase) so that
  Traditional/Simplified, half/full-width, and case variants all match. The
  ngram tokenizer is case-sensitive, so normalization is what makes search
  case-insensitive.
- **Change-driven sync** — the ORM before_flush/after_flush hooks
  (``app/services/work_search_events.py``) write ``fts_outbox`` rows in the
  same transaction as the base-row change; the per-minute-ish drain
  (``drain_fts_outbox``) replays them onto the sidecar (idempotent full-state
  DELETE+INSERT). The hourly ``reconcile_fts`` diff is a backstop for paths
  that bypass the outbox (scripts, direct SQL).
- **Candidate retrieval** — ``fts_match`` retrieves candidates; callers
  compute ``similarity_score`` for precise ranking. Single-character queries
  (ngram produces no tokens below ``min_token_size``=2) fall back to a Python
  scan of the base tables.

On PostgreSQL there is no sidecar: searches match the in-table ``search_text``
column via ``pg_trgm`` GIN. ``_search_pg_like`` replicates the ngram tokenizer
semantics (whitespace-split, ≥2-char tokens OR-ed as literal substrings) so the
candidate set matches the Turso sidecar for CJK and English alike.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.services.text_normalizer import normalize_title

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sidecar engine
# ---------------------------------------------------------------------------

_FTS_ENGINE = None


def _sidecar_url() -> str:
    """Derive the FTS sidecar database URL from the main DATABASE_URL."""
    url = settings.database_url
    path = url.split(":///", 1)[-1].split("?", 1)[0]
    if path == ":memory:":
        path = "data/rss_ripple_fts.db"
    stem, dot, _ext = path.rpartition(".")
    fts_path = f"{stem}_fts.db" if dot else f"{path}_fts.db"
    return f"sqlite+aioturso:///{fts_path}?experimental_features=index_method"


def _get_fts_engine():
    """Lazily create the sidecar engine (tests may inject their own)."""
    global _FTS_ENGINE
    if _FTS_ENGINE is None:
        _FTS_ENGINE = create_async_engine(_sidecar_url())
    return _FTS_ENGINE


def _fts_available(db: Any) -> bool:
    """FTS sidecar only exists alongside the Turso backend."""
    engine = getattr(db, "engine", None)  # AsyncSession
    if engine is None:
        sync_session = getattr(db, "sync_session", None)
        if sync_session is not None:
            engine = sync_session.get_bind()
        else:
            engine = db
    url = str(engine.url) if engine is not None else settings.database_url
    return "turso" in url


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SHADOW_TABLES = ("tv_series_fts", "movie_fts", "audio_work_fts")

_CREATE_SHADOW = """
    CREATE TABLE IF NOT EXISTS {table} (
        entity_id TEXT PRIMARY KEY,
        title_cn TEXT,
        title_en TEXT,
        original_title TEXT,
        aliases TEXT
    )
"""

_CREATE_INDEX = """
    CREATE INDEX IF NOT EXISTS {table}_idx ON {table}
    USING fts (title_cn, title_en, original_title, aliases)
    WITH (tokenizer = 'ngram')
"""


async def ensure_fts_tables() -> None:
    """Create FTS shadow tables and indexes on the sidecar (Turso only)."""
    if "turso" not in settings.database_url and _FTS_ENGINE is None:
        return
    engine = _get_fts_engine()
    async with engine.begin() as conn:
        for table in _SHADOW_TABLES:
            try:
                await conn.execute(text(_CREATE_SHADOW.format(table=table)))
                await conn.execute(text(_CREATE_INDEX.format(table=table)))
            except Exception as e:
                logger.warning("[fts] Could not create FTS objects for %s: %s", table, e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fts_values(entity: Any) -> dict[str, str]:
    """Extract normalized title fields from a TVSeries or Movie for FTS indexing."""
    return {
        "title_cn": normalize_title(entity.title_cn) or "",
        "title_en": normalize_title(entity.title_en) or "",
        "original_title": normalize_title(entity.original_title) or "",
        "aliases": " ".join(
            normalize_title(a) for a in (entity.aliases or []) if a
        ),
    }


async def _shadow_write(statements: list[tuple[str, dict]]) -> None:
    """Execute write statements against the sidecar in one transaction."""
    engine = _get_fts_engine()
    async with engine.begin() as conn:
        for sql, params in statements:
            await conn.execute(text(sql), params)


async def _upsert(table: str, entity: Any) -> None:
    vals = _fts_values(entity)
    vals["id"] = entity.id
    await _shadow_write([
        (f"DELETE FROM {table} WHERE entity_id = :id", {"id": entity.id}),
        (
            f"INSERT INTO {table} (entity_id, title_cn, title_en, original_title, aliases) "
            "VALUES (:id, :title_cn, :title_en, :original_title, :aliases)",
            vals,
        ),
    ])


async def _delete(table: str, entity_id: str) -> None:
    await _shadow_write([(f"DELETE FROM {table} WHERE entity_id = :id", {"id": entity_id})])


async def _search_fts(table: str, norm: str, limit: int) -> list[str]:
    """fts_match over a shadow table. Callers rank by similarity themselves,
    so no relevance ordering is applied here."""
    engine = _get_fts_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                f"SELECT entity_id FROM {table} "
                "WHERE fts_match(title_cn, title_en, original_title, aliases, :query) "
                "LIMIT :limit"
            ),
            {"query": norm, "limit": limit},
        )
        return [row[0] for row in result.fetchall()]


async def _search_entities_like(db: AsyncSession, model: Any, norm: str, limit: int) -> list[str]:
    """FTS-less substring search over a work table (single-char Turso queries).

    The ngram tokenizer cannot produce tokens for queries shorter than its
    ``min_token_size`` (2), so ``fts_match`` returns nothing for a single CJK
    character. Fall back to scanning the (small) work table and matching the
    normalized query against the normalized titles/aliases in Python — same
    normalization as the FTS indexed content, so matching semantics stay
    consistent across backends.
    """
    ids: list[str] = []
    try:
        result = await db.execute(select(model))
        entities = result.scalars().all()
    except Exception as e:
        logger.warning("[fts] LIKE fallback search failed: %s", e)
        return []
    for e in entities:
        haystack = " ".join(filter(None, [
            normalize_title(e.title_cn),
            normalize_title(e.title_en),
            normalize_title(e.original_title),
            " ".join(normalize_title(a) for a in (e.aliases or []) if a),
        ]))
        if norm in haystack:
            ids.append(e.id)
            if len(ids) >= limit:
                break
    return ids


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a literal substring never mis-matches.

    ``search_text`` is already normalized (NFKC + OpenCC t2s + lowercase), so
    case folding happens on both sides and a plain ``LIKE`` is equivalent to
    ``ILIKE``. Titles/aliases can legitimately contain ``%``/``_``/``\\``, so
    the query term must be escaped or those characters would turn into
    wildcards and silently change the match set.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _search_pg_like(db: AsyncSession, model: Any, norm: str, limit: int) -> list[str]:
    """Indexed substring search over ``search_text`` (PostgreSQL).

    ``search_text`` holds the normalized title concatenation maintained by the
    ORM before_flush hook; the ``pg_trgm`` GIN index accelerates the
    ``LIKE '%q%'`` pattern. Same normalization as the Turso FTS indexed
    content, so matching semantics stay consistent across backends.

    Matching mirrors the Turso ``ngram`` tokenizer (``min_token_size``=2):
    the normalized query is split on whitespace and each token with ≥2
    characters is matched as a literal substring (``LIKE '%tok%'``), with the
    tokens OR-ed together. For CJK titles (no whitespace) this degenerates to
    an exact substring match — the same result as Turso's contiguous
    AND-of-bigrams. A single-character query (Turso's Python-scan fallback) is
    matched as a substring too.
    """
    try:
        tokens = norm.split()
        if len(tokens) == 1:
            # Single token (CJK title, single English word, or a single
            # character): contiguous substring match — Turso's single-token
            # AND-of-bigrams, or its single-char Python-scan fallback.
            patterns = tokens
        else:
            # Multi-word query: OR the ≥2-char tokens (Turso's ngram
            # ``min_token_size``=2 drops 1-char tokens entirely).
            patterns = [t for t in tokens if len(t) >= 2]
            if not patterns:
                return []
        conditions = [
            model.search_text.like(f"%{_escape_like(t)}%", escape="\\")
            for t in patterns
        ]
        stmt = select(model.id).where(or_(*conditions)).limit(limit)
        result = await db.execute(stmt)
        return [row[0] for row in result.all()]
    except Exception as e:
        logger.warning("[fts] search_text LIKE search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Series FTS
# ---------------------------------------------------------------------------


async def upsert_series_fts(db: AsyncSession, series: Any) -> None:
    """Insert or update a series in the FTS index."""
    if not _fts_available(db):
        return
    try:
        await _upsert("tv_series_fts", series)
    except Exception as e:
        logger.warning("[fts] upsert_series_fts failed for %s: %s", series.id, e)


async def delete_series_fts(db: AsyncSession, series_id: str) -> None:
    """Remove a series from the FTS index."""
    if not _fts_available(db):
        return
    try:
        await _delete("tv_series_fts", series_id)
    except Exception as e:
        logger.warning("[fts] delete_series_fts failed for %s: %s", series_id, e)


async def _drain_pending_changes(db: AsyncSession) -> None:
    """Best-effort replay of pending ``fts_outbox`` rows before an FTS read.

    Search must reflect work rows committed moments ago even when the
    periodic 30s drain job has not fired yet — e.g. a manual link followed
    immediately by a manual search, or deployments with the scheduler
    disabled. ``drain_fts_outbox`` is idempotent (full-state shadow writes)
    and no-ops on an empty outbox / non-Turso backends, so this is one cheap
    SELECT on the common read path.
    """
    try:
        await drain_fts_outbox(db)
    except Exception as e:
        logger.warning("[fts] pre-search drain failed: %s", e)


async def search_series_fts(
    db: AsyncSession, query: str, limit: int = 30
) -> list[str]:
    """Search series by title. Returns a list of series entity IDs."""
    norm = normalize_title(query)
    if not norm:
        return []
    await _drain_pending_changes(db)
    if not _fts_available(db):
        from app.models.series import TVSeries

        return await _search_pg_like(db, TVSeries, norm, limit)
    if len(norm) < 2:
        # ngram tokenizer emits no tokens for single-character queries.
        from app.models.series import TVSeries

        return await _search_entities_like(db, TVSeries, norm, limit)
    try:
        return await _search_fts("tv_series_fts", norm, limit)
    except Exception as e:
        logger.warning("[fts] search_series_fts failed for %r: %s", norm[:60], e)
        return []


async def rebuild_series_fts(db: AsyncSession) -> int:
    """Rebuild the entire series FTS index from the tv_series table."""
    if not _fts_available(db):
        return 0
    from app.models.series import TVSeries

    entities = (await db.execute(select(TVSeries))).scalars().all()
    statements: list[tuple[str, dict]] = [("DELETE FROM tv_series_fts", {})]
    for series in entities:
        vals = _fts_values(series)
        vals["id"] = series.id
        statements.append((
            "INSERT INTO tv_series_fts (entity_id, title_cn, title_en, original_title, aliases) "
            "VALUES (:id, :title_cn, :title_en, :original_title, :aliases)",
            vals,
        ))
    try:
        await _shadow_write(statements)
    except Exception as e:
        logger.warning("[fts] rebuild_series_fts failed: %s", e)
        return 0
    return len(entities)


# ---------------------------------------------------------------------------
# Movie FTS
# ---------------------------------------------------------------------------


async def upsert_movie_fts(db: AsyncSession, movie: Any) -> None:
    """Insert or update a movie in the FTS index."""
    if not _fts_available(db):
        return
    try:
        await _upsert("movie_fts", movie)
    except Exception as e:
        logger.warning("[fts] upsert_movie_fts failed for %s: %s", movie.id, e)


async def delete_movie_fts(db: AsyncSession, movie_id: str) -> None:
    """Remove a movie from the FTS index."""
    if not _fts_available(db):
        return
    try:
        await _delete("movie_fts", movie_id)
    except Exception as e:
        logger.warning("[fts] delete_movie_fts failed for %s: %s", movie_id, e)


async def search_movie_fts(
    db: AsyncSession, query: str, limit: int = 30
) -> list[str]:
    """Search movies by title. Returns a list of movie entity IDs."""
    norm = normalize_title(query)
    if not norm:
        return []
    await _drain_pending_changes(db)
    if not _fts_available(db):
        from app.models.movie import Movie

        return await _search_pg_like(db, Movie, norm, limit)
    if len(norm) < 2:
        # ngram tokenizer emits no tokens for single-character queries.
        from app.models.movie import Movie

        return await _search_entities_like(db, Movie, norm, limit)
    try:
        return await _search_fts("movie_fts", norm, limit)
    except Exception as e:
        logger.warning("[fts] search_movie_fts failed for %r: %s", norm[:60], e)
        return []


async def rebuild_movie_fts(db: AsyncSession) -> int:
    """Rebuild the entire movie FTS index from the movies table."""
    if not _fts_available(db):
        return 0
    from app.models.movie import Movie

    entities = (await db.execute(select(Movie))).scalars().all()
    statements: list[tuple[str, dict]] = [("DELETE FROM movie_fts", {})]
    for movie in entities:
        vals = _fts_values(movie)
        vals["id"] = movie.id
        statements.append((
            "INSERT INTO movie_fts (entity_id, title_cn, title_en, original_title, aliases) "
            "VALUES (:id, :title_cn, :title_en, :original_title, :aliases)",
            vals,
        ))
    try:
        await _shadow_write(statements)
    except Exception as e:
        logger.warning("[fts] rebuild_movie_fts failed: %s", e)
        return 0
    return len(entities)


# ---------------------------------------------------------------------------
# AudioWork FTS
# ---------------------------------------------------------------------------


async def upsert_audio_work_fts(db: AsyncSession, audio_work: Any) -> None:
    """Insert or update an audio work in the FTS index."""
    if not _fts_available(db):
        return
    try:
        await _upsert("audio_work_fts", audio_work)
    except Exception as e:
        logger.warning("[fts] upsert_audio_work_fts failed for %s: %s", audio_work.id, e)


async def delete_audio_work_fts(db: AsyncSession, audio_work_id: str) -> None:
    """Remove an audio work from the FTS index."""
    if not _fts_available(db):
        return
    try:
        await _delete("audio_work_fts", audio_work_id)
    except Exception as e:
        logger.warning("[fts] delete_audio_work_fts failed for %s: %s", audio_work_id, e)


async def search_audio_work_fts(
    db: AsyncSession, query: str, limit: int = 30
) -> list[str]:
    """Search audio works by title. Returns a list of audio work entity IDs."""
    norm = normalize_title(query)
    if not norm:
        return []
    await _drain_pending_changes(db)
    if not _fts_available(db):
        from app.models.audio_work import AudioWork

        return await _search_pg_like(db, AudioWork, norm, limit)
    if len(norm) < 2:
        # ngram tokenizer emits no tokens for single-character queries.
        from app.models.audio_work import AudioWork

        return await _search_entities_like(db, AudioWork, norm, limit)
    try:
        return await _search_fts("audio_work_fts", norm, limit)
    except Exception as e:
        logger.warning("[fts] search_audio_work_fts failed for %r: %s", norm[:60], e)
        return []


async def rebuild_audio_work_fts(db: AsyncSession) -> int:
    """Rebuild the entire audio work FTS index from the audio_works table."""
    if not _fts_available(db):
        return 0
    from app.models.audio_work import AudioWork

    entities = (await db.execute(select(AudioWork))).scalars().all()
    statements: list[tuple[str, dict]] = [("DELETE FROM audio_work_fts", {})]
    for aw in entities:
        vals = _fts_values(aw)
        vals["id"] = aw.id
        statements.append((
            "INSERT INTO audio_work_fts (entity_id, title_cn, title_en, original_title, aliases) "
            "VALUES (:id, :title_cn, :title_en, :original_title, :aliases)",
            vals,
        ))
    try:
        await _shadow_write(statements)
    except Exception as e:
        logger.warning("[fts] rebuild_audio_work_fts failed: %s", e)
        return 0
    return len(entities)


async def backfill_fts_if_empty(db: AsyncSession) -> None:
    """Populate FTS shadow tables from the base tables when they are empty.

    One-time recovery for databases created before the FTS indexes were
    introduced (or freshly migrated from SQLite).
    """
    if not _fts_available(db):
        return
    engine = _get_fts_engine()
    for table, rebuild in (
        ("tv_series_fts", rebuild_series_fts),
        ("movie_fts", rebuild_movie_fts),
        ("audio_work_fts", rebuild_audio_work_fts),
    ):
        try:
            async with engine.connect() as conn:
                count = (await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar() or 0
        except Exception:
            continue
        if count == 0:
            n = await rebuild(db)
            if n:
                logger.info("[fts] backfilled %s with %d rows", table, n)


async def drain_fts_outbox(db: AsyncSession, limit: int = 500) -> int:
    """Replay ``fts_outbox`` change rows onto the sidecar shadow tables.

    Claims a batch (reads + removes from the main DB in the caller's
    transaction), then writes the resulting DELETE/INSERT statements to the
    sidecar best-effort. Shadow writes are full-state replaces, so replaying
    the same entity twice — or the same batch out of order — always converges
    to the latest op. A failed sidecar write is logged and left for the
    reconcile job; outbox rows are consumed regardless (they would replay the
    same state anyway).

    ``limit`` is the per-tick batch size; repeated rows for one entity within
    a batch collapse naturally since every op is a full-state write.
    """
    if "turso" not in settings.database_url:
        return 0
    from app.models.audio_work import AudioWork
    from app.models.fts_outbox import FtsOutbox
    from app.models.movie import Movie
    from app.models.series import TVSeries

    rows = (await db.execute(
        select(FtsOutbox).order_by(FtsOutbox.created_at).limit(limit)
    )).scalars().all()
    if not rows:
        return 0

    models = {"series": TVSeries, "movie": Movie, "audio": AudioWork}
    tables = {"series": "tv_series_fts", "movie": "movie_fts", "audio": "audio_work_fts"}
    upsert_sql = {
        t: (
            f"INSERT INTO {t} (entity_id, title_cn, title_en, original_title, aliases) "
            "VALUES (:id, :title_cn, :title_en, :original_title, :aliases)"
        )
        for t in ("tv_series_fts", "movie_fts", "audio_work_fts")
    }

    statements: list[tuple[str, dict]] = []
    for r in rows:
        table = tables[r.entity_type]
        delete_stmt = (f"DELETE FROM {table} WHERE entity_id = :id", {"id": r.entity_id})
        if r.op != "upsert":
            statements.append(delete_stmt)
            continue
        entity = await db.get(
            models[r.entity_type], r.entity_id, populate_existing=True
        )
        if entity is None:
            # Deleted before the drain got to it — mirror the deletion.
            statements.append(delete_stmt)
            continue
        vals = _fts_values(entity)
        vals["id"] = r.entity_id
        statements.append(delete_stmt)
        statements.append((upsert_sql[table], vals))

    await db.execute(delete(FtsOutbox).where(FtsOutbox.id.in_([r.id for r in rows])))
    try:
        await _shadow_write(statements)
    except Exception as e:
        logger.warning(
            "[fts] drain write failed for %d rows (reconcile will heal): %s", len(rows), e
        )
    return len(rows)


async def backfill_search_text(db: AsyncSession) -> int:
    """Compute ``search_text`` for work rows where it is NULL.

    One-time recovery for databases created before the ``search_text`` column
    (and the ORM before_flush hook that maintains it) existed. Runs on both
    backends: PostgreSQL needs it for the ``pg_trgm`` GIN indexes; on Turso it
    keeps the column meaningful (matching itself uses the sidecar or the
    single-char Python scan). Idempotent.
    """
    from app.models.audio_work import AudioWork
    from app.models.movie import Movie
    from app.models.series import TVSeries
    from app.services.work_search_events import build_search_text

    updated = 0
    for model in (TVSeries, Movie, AudioWork):
        rows = (await db.execute(
            select(model).where(model.search_text.is_(None))
        )).scalars().all()
        for entity in rows:
            entity.search_text = build_search_text(entity)
            updated += 1
    if updated:
        logger.info("[fts] backfilled search_text on %d rows", updated)
    return updated


# ---------------------------------------------------------------------------
# Reconciliation (periodic safety net)
# ---------------------------------------------------------------------------


async def reconcile_fts(db: AsyncSession) -> dict[str, int]:
    """Diff base tables against FTS shadow tables and fix any divergence.

    The upsert/delete call sites cover the hot paths; this reconciliation
    pass heals everything they miss (scripts, metadata dedup merges, direct
    SQL, swallowed write failures). Cheap at the current table sizes
    (hundreds of rows), so it simply compares both sides in full:

    - missing or content-stale shadow rows → rewrite
    - orphan shadow rows (base row gone) → delete
    """
    if not _fts_available(db):
        return {"updated": 0, "deleted": 0}
    from app.models.audio_work import AudioWork
    from app.models.movie import Movie
    from app.models.series import TVSeries

    report = {"updated": 0, "deleted": 0}
    engine = _get_fts_engine()
    for table, model in (
        ("tv_series_fts", TVSeries),
        ("movie_fts", Movie),
        ("audio_work_fts", AudioWork),
    ):
        entities = (await db.execute(select(model))).scalars().all()
        expected = {e.id: _fts_values(e) for e in entities}
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                f"SELECT entity_id, title_cn, title_en, original_title, aliases FROM {table}"
            ))).fetchall()
        current = {
            r[0]: {
                "title_cn": r[1] or "",
                "title_en": r[2] or "",
                "original_title": r[3] or "",
                "aliases": r[4] or "",
            }
            for r in rows
        }

        statements: list[tuple[str, dict]] = []
        insert_sql = (
            f"INSERT INTO {table} (entity_id, title_cn, title_en, original_title, aliases) "
            "VALUES (:id, :title_cn, :title_en, :original_title, :aliases)"
        )
        for eid, vals in expected.items():
            if current.get(eid) != vals:
                statements.append((f"DELETE FROM {table} WHERE entity_id = :id", {"id": eid}))
                row = dict(vals)
                row["id"] = eid
                statements.append((insert_sql, row))
                report["updated"] += 1
        for eid in current:
            if eid not in expected:
                statements.append((f"DELETE FROM {table} WHERE entity_id = :id", {"id": eid}))
                report["deleted"] += 1
        if statements:
            try:
                await _shadow_write(statements)
            except Exception as e:
                logger.warning("[fts] reconcile failed for %s: %s", table, e)
    return report
