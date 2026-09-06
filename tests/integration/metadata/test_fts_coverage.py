"""In-process coverage for ``app.services.fts`` branches the outbox unit
tests do not reach.

Targets (per data/coverage-report.txt missing lines):

- sidecar URL derivation (``:memory:`` fallback, extensionless paths) and
  ``_fts_available`` engine/settings fallbacks
- ``ensure_fts_tables`` non-Turso early return and per-table failure swallow
- ``_upsert``/``_delete`` round-trips plus the error-swallowing wrappers for
  all three work kinds
- empty-query early returns, the single-char Python-scan fallback (incl. its
  limit and failure branches), and ``_search_pg_like`` token semantics
  (multi-token OR, sub-2-char token drop, LIKE escaping, failure swallow)
- ``rebuild_*`` happy / failure / non-Turso paths
- ``backfill_fts_if_empty`` (populate, missing-table skip, non-Turso no-op)
- ``drain_fts_outbox`` non-Turso gate and sidecar-write failure
- ``backfill_search_text`` across all three work tables
- ``reconcile_fts`` stale rewrite / orphan delete / write failure / non-Turso
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text, update

from app.config import settings
from app.models.audio_work import AudioWork
from app.models.fts_outbox import FtsOutbox
from app.models.movie import Movie
from app.models.series import TVSeries
from app.services import fts

# Some tests assume the MAIN database is Turso (sidecar gated on
# settings.database_url). The distributed stack runs PostgreSQL — skip there.
_MAIN_TURSO = settings.database_url.startswith("sqlite")
requires_turso_main = pytest.mark.skipif(
    not _MAIN_TURSO, reason="main DB is PostgreSQL (distributed stack)"
)


def _series(**kw) -> TVSeries:
    defaults = dict(
        id=str(uuid.uuid4()),
        title_cn="测试剧集",
        title_en="Test Series",
        original_title="Test Series",
        aliases=["别名"],
        external_id="tt-test-series",
        external_source="manual",
        content_type="tv",
    )
    defaults.update(kw)
    return TVSeries(**defaults)


def _movie(**kw) -> Movie:
    defaults = dict(
        id=str(uuid.uuid4()),
        title_cn="测试电影",
        title_en="Test Movie",
        original_title="Test Movie",
        external_id="tt-test-movie",
        external_source="manual",
        content_type="movie",
    )
    defaults.update(kw)
    return Movie(**defaults)


def _audio_work(**kw) -> AudioWork:
    defaults = dict(
        id=str(uuid.uuid4()),
        title_cn="深夜音声作品",
        title_en="Late Night Audio",
        original_title="Late Night Audio",
        aliases=["音声别名"],
        external_source="manual",
        content_type="asmr",
    )
    defaults.update(kw)
    return AudioWork(**defaults)


async def _shadow_rows(table: str) -> dict[str, str]:
    engine = fts._get_fts_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text(f"SELECT entity_id, title_cn FROM {table}"))
        ).fetchall()
    return {r[0]: (r[1] or "") for r in rows}


# ---------------------------------------------------------------------------
# URL derivation / availability
# ---------------------------------------------------------------------------


def test_sidecar_url_memory_and_path_variants(monkeypatch):
    # In-memory main DB: no file stem to derive from → fixed data/ fallback
    # (whose own stem is then suffixed like any other path).
    monkeypatch.setattr(settings, "database_url", "sqlite+aioturso:///:memory:")
    url = fts._sidecar_url()
    assert url == (
        "sqlite+aioturso:///data/rss_ripple_fts_fts.db"
        "?experimental_features=index_method"
    )
    # Extensionless path: stem/rpartition finds no dot.
    monkeypatch.setattr(settings, "database_url", "sqlite+aioturso:///tmp/x/maindb")
    assert "/tmp/x/maindb_fts.db" in fts._sidecar_url()
    # Dotted path: extension replaced.
    monkeypatch.setattr(settings, "database_url", "sqlite+aioturso:///tmp/x/main.db")
    assert "/tmp/x/main_fts.db" in fts._sidecar_url()


@requires_turso_main
def test_fts_available_engine_and_settings_fallback(db_engine, monkeypatch):
    # An AsyncEngine has no .engine/.sync_session — it becomes its own bind.
    assert fts._fts_available(db_engine) is True
    # db=None: engine resolution fails entirely → settings DATABASE_URL decides.
    assert fts._fts_available(None) is True  # test settings are Turso
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://db/x")
    assert fts._fts_available(None) is False


# ---------------------------------------------------------------------------
# ensure_fts_tables
# ---------------------------------------------------------------------------


async def test_ensure_fts_tables_skips_non_turso_without_engine(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://db/x")
    monkeypatch.setattr(fts, "_FTS_ENGINE", None)
    await fts.ensure_fts_tables()
    # Returned before any engine was created.
    assert fts._FTS_ENGINE is None


async def test_ensure_fts_tables_swallows_per_table_errors(db_engine):
    # Replace one shadow table with a view: the CREATE TABLE/CREATE INDEX for
    # it must fail and be logged, not propagated.
    engine = fts._get_fts_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS movie_fts"))
        await conn.execute(text("CREATE VIEW movie_fts AS SELECT 'x' AS entity_id"))
    await fts.ensure_fts_tables()  # must not raise
    # The other shadow tables were still (re)created fine.
    async with engine.connect() as conn:
        n = (
            await conn.execute(text("SELECT COUNT(*) FROM tv_series_fts"))
        ).scalar_one()
    assert n == 0
    # Clean up so later sidecar users in this test see a real table again.
    async with engine.begin() as conn:
        await conn.execute(text("DROP VIEW movie_fts"))
    await fts.ensure_fts_tables()
    async with engine.connect() as conn:
        n = (await conn.execute(text("SELECT COUNT(*) FROM movie_fts"))).scalar_one()
    assert n == 0


# ---------------------------------------------------------------------------
# upsert / delete
# ---------------------------------------------------------------------------


async def test_upsert_delete_roundtrip(db_session):
    series = _series()
    db_session.add(series)
    await db_session.flush()
    await fts.upsert_series_fts(db_session, series)
    assert series.id in await fts._search_fts("tv_series_fts", "测试剧集", 10)
    await fts.delete_series_fts(db_session, series.id)
    assert series.id not in await fts._search_fts("tv_series_fts", "测试剧集", 10)


async def test_upsert_delete_wrappers_swallow_sidecar_errors(
    db_session, monkeypatch
):
    async def _boom(*a, **kw):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(fts, "_upsert", _boom)
    monkeypatch.setattr(fts, "_delete", _boom)
    series, movie, aw = _series(), _movie(), _audio_work()
    db_session.add_all([series, movie, aw])
    await db_session.flush()

    # None of these may propagate the sidecar failure.
    await fts.upsert_series_fts(db_session, series)
    await fts.delete_series_fts(db_session, series.id)
    await fts.upsert_movie_fts(db_session, movie)
    await fts.delete_movie_fts(db_session, movie.id)
    await fts.upsert_audio_work_fts(db_session, aw)
    await fts.delete_audio_work_fts(db_session, aw.id)


async def test_upsert_delete_wrappers_noop_when_unavailable(
    db_session, monkeypatch
):
    series, movie, aw = _series(), _movie(), _audio_work()
    db_session.add_all([series, movie, aw])
    await db_session.flush()
    calls = []

    async def _spy(*a, **kw):
        calls.append(a)

    monkeypatch.setattr(fts, "_fts_available", lambda db: False)
    monkeypatch.setattr(fts, "_upsert", _spy)
    monkeypatch.setattr(fts, "_delete", _spy)
    await fts.upsert_series_fts(db_session, series)
    await fts.delete_series_fts(db_session, series.id)
    await fts.upsert_movie_fts(db_session, movie)
    await fts.delete_movie_fts(db_session, movie.id)
    await fts.upsert_audio_work_fts(db_session, aw)
    await fts.delete_audio_work_fts(db_session, aw.id)
    assert calls == []


# ---------------------------------------------------------------------------
# search paths
# ---------------------------------------------------------------------------


async def test_search_empty_query_returns_early(db_session):
    assert await fts.search_series_fts(db_session, "   ") == []
    assert await fts.search_movie_fts(db_session, "") == []
    assert await fts.search_audio_work_fts(db_session, "  ") == []


async def test_single_char_fallback_scan(db_session):
    series, movie, aw = _series(), _movie(), _audio_work()
    db_session.add_all([series, movie, aw])
    await db_session.flush()

    assert series.id in await fts.search_series_fts(db_session, "试")
    assert movie.id in await fts.search_movie_fts(db_session, "电")
    assert aw.id in await fts.search_audio_work_fts(db_session, "声")
    # No match → empty, not an error.
    assert await fts.search_series_fts(db_session, "革") == []


async def test_single_char_fallback_respects_limit(db_session):
    db_session.add(_series())
    for i in range(2):
        db_session.add(TVSeries(id=str(uuid.uuid4()), title_cn=f"测试副本{i}"))
    await db_session.flush()
    # Three rows contain "试" but the scan stops at the limit.
    ids = await fts.search_series_fts(db_session, "试", limit=2)
    assert len(ids) == 2


async def test_single_char_fallback_scan_db_failure_returns_empty():
    class _BadDB:
        url = "sqlite+aioturso:///x.db"

        async def execute(self, *a, **kw):
            raise RuntimeError("db gone")

    # The pre-search drain failure is swallowed too (search still attempted).
    assert await fts.search_series_fts(_BadDB(), "试") == []


async def test_search_swallows_fts_errors(db_session, monkeypatch):
    db_session.add(_series())
    await db_session.flush()

    async def _boom(*a, **kw):
        raise RuntimeError("fts broken")

    monkeypatch.setattr(fts, "_search_fts", _boom)
    assert await fts.search_series_fts(db_session, "测试剧集") == []
    assert await fts.search_movie_fts(db_session, "测试电影") == []
    assert await fts.search_audio_work_fts(db_session, "音声") == []


# ---------------------------------------------------------------------------
# _search_pg_like (PostgreSQL path, exercised over the test session)
# ---------------------------------------------------------------------------


async def test_pg_like_token_semantics(db_session, monkeypatch):
    series = _series()
    db_session.add(series)
    await db_session.flush()
    monkeypatch.setattr(fts, "_fts_available", lambda db: False)
    # Single CJK token → contiguous substring.
    assert series.id in await fts.search_series_fts(db_session, "测试")
    # Multi-token: ≥2-char tokens OR-ed, 1-char tokens dropped ("试" ignored,
    # "test" still matches the normalized search_text).
    assert series.id in await fts.search_series_fts(db_session, "test 试")
    # Every token below the ngram min size → no criteria → empty.
    assert await fts.search_series_fts(db_session, "a b") == []


async def test_pg_like_escapes_like_wildcards(db_session, monkeypatch):
    monkeypatch.setattr(fts, "_fts_available", lambda db: False)
    m = Movie(
        id=str(uuid.uuid4()),
        title_cn="百分百电影",
        title_en="100% Show",
        external_source="manual",
        content_type="movie",
    )
    db_session.add(m)
    await db_session.flush()
    # "%" must be matched literally, not as a LIKE wildcard.
    assert m.id in await fts.search_movie_fts(db_session, "100%")
    other = Movie(
        id=str(uuid.uuid4()),
        title_en="1000 Shows",
        external_source="manual",
        content_type="movie",
    )
    db_session.add(other)
    await db_session.flush()
    assert other.id not in await fts.search_movie_fts(db_session, "100%")


async def test_pg_like_db_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(fts, "_fts_available", lambda db: False)

    class _BadDB:
        url = "sqlite+aioturso:///x.db"

        async def execute(self, *a, **kw):
            raise RuntimeError("db gone")

    assert await fts.search_movie_fts(_BadDB(), "test") == []


async def test_pg_like_audio_work_path(db_session, monkeypatch):
    monkeypatch.setattr(fts, "_fts_available", lambda db: False)
    aw = _audio_work()
    db_session.add(aw)
    await db_session.flush()
    assert aw.id in await fts.search_audio_work_fts(db_session, "音声")
    assert await fts.search_audio_work_fts(db_session, "不存在") == []


# ---------------------------------------------------------------------------
# rebuild_*
# ---------------------------------------------------------------------------


async def test_rebuild_all_three_work_kinds(db_session):
    series, movie, aw = _series(), _movie(), _audio_work()
    db_session.add_all([series, movie, aw])
    await db_session.flush()

    assert await fts.rebuild_series_fts(db_session) == 1
    assert await fts.rebuild_movie_fts(db_session) == 1
    assert await fts.rebuild_audio_work_fts(db_session) == 1

    # Rebuild writes the sidecar directly — queryable without any drain.
    assert series.id in await fts._search_fts("tv_series_fts", "测试剧集", 10)
    assert movie.id in await fts._search_fts("movie_fts", "测试电影", 10)
    assert aw.id in await fts._search_fts("audio_work_fts", "音声", 10)


async def test_rebuild_non_turso_returns_zero(db_session, monkeypatch):
    monkeypatch.setattr(fts, "_fts_available", lambda db: False)
    assert await fts.rebuild_series_fts(db_session) == 0
    assert await fts.rebuild_movie_fts(db_session) == 0
    assert await fts.rebuild_audio_work_fts(db_session) == 0


async def test_rebuild_swallows_sidecar_errors(db_session, monkeypatch):
    db_session.add(_series())
    await db_session.flush()

    async def _boom(*a, **kw):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(fts, "_shadow_write", _boom)
    assert await fts.rebuild_series_fts(db_session) == 0
    assert await fts.rebuild_movie_fts(db_session) == 0
    assert await fts.rebuild_audio_work_fts(db_session) == 0


# ---------------------------------------------------------------------------
# backfill_fts_if_empty
# ---------------------------------------------------------------------------


async def test_backfill_fts_if_empty_populates_shadow(db_session):
    series = _series()
    db_session.add(series)
    await db_session.flush()
    # The ORM hook enqueued an outbox row, but nothing drained it — the
    # sidecar is still empty, so the backfill must rebuild from base tables.
    assert await _shadow_rows("tv_series_fts") == {}
    await fts.backfill_fts_if_empty(db_session)
    assert series.id in await fts._search_fts("tv_series_fts", "测试剧集", 10)


async def test_backfill_fts_if_empty_non_turso_noop(db_session, monkeypatch):
    monkeypatch.setattr(fts, "_fts_available", lambda db: False)
    await fts.backfill_fts_if_empty(db_session)  # returns immediately
    assert await _shadow_rows("tv_series_fts") == {}


async def test_backfill_skips_missing_shadow_table(db_session):
    series = _series()
    db_session.add(series)
    await db_session.flush()
    engine = fts._get_fts_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE movie_fts"))
    # The COUNT(*) on the missing table fails and is skipped; the other
    # tables are still backfilled.
    await fts.backfill_fts_if_empty(db_session)
    assert series.id in await fts._search_fts("tv_series_fts", "测试剧集", 10)


# ---------------------------------------------------------------------------
# drain_fts_outbox gates/failures (happy path lives in tests/unit)
# ---------------------------------------------------------------------------


@requires_turso_main
async def test_drain_non_turso_gate(db_session, monkeypatch):
    series = _series()
    db_session.add(series)
    await db_session.flush()
    # The flush above already enqueued an outbox row (Turso settings); the
    # gate must leave it untouched on a non-Turso URL.
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://db/x")
    assert await fts.drain_fts_outbox(db_session) == 0
    n = (
        await db_session.execute(select(func.count()).select_from(FtsOutbox))
    ).scalar_one()
    assert n == 1


@requires_turso_main
async def test_drain_shadow_write_failure_still_consumes_rows(
    db_session, monkeypatch
):
    db_session.add(_series())
    await db_session.flush()

    async def _boom(*a, **kw):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(fts, "_shadow_write", _boom)
    n = await fts.drain_fts_outbox(db_session)
    assert n == 1
    # Outbox rows are consumed regardless; reconcile heals the sidecar later.
    remaining = (
        await db_session.execute(select(func.count()).select_from(FtsOutbox))
    ).scalar_one()
    assert remaining == 0


# ---------------------------------------------------------------------------
# backfill_search_text
# ---------------------------------------------------------------------------


async def test_backfill_search_text_fills_all_work_tables(db_session):
    series, movie, aw = _series(), _movie(), _audio_work()
    db_session.add_all([series, movie, aw])
    await db_session.flush()
    # Null the column behind the ORM's back (simulating pre-hook databases).
    for model in (TVSeries, Movie, AudioWork):
        await db_session.execute(
            update(model)
            .values(search_text=None)
            .execution_options(synchronize_session=False)
        )
    # Drop the stale in-memory copies so the backfill loads the NULL rows
    # fresh and its assignment actually marks them dirty.
    db_session.expire_all()

    n = await fts.backfill_search_text(db_session)
    assert n == 3
    await db_session.flush()
    for model, wid in (
        (TVSeries, series.id),
        (Movie, movie.id),
        (AudioWork, aw.id),
    ):
        row = (
            await db_session.execute(
                select(model)
                .where(model.id == wid)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.search_text


# ---------------------------------------------------------------------------
# reconcile_fts
# ---------------------------------------------------------------------------


async def test_reconcile_heals_stale_and_orphan_rows(db_session):
    series = _series()
    db_session.add(series)
    await db_session.flush()
    await fts.drain_fts_outbox(db_session)
    engine = fts._get_fts_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE tv_series_fts SET title_cn='stale' WHERE entity_id=:i"),
            {"i": series.id},
        )
        await conn.execute(
            text(
                "INSERT INTO tv_series_fts "
                "(entity_id, title_cn, title_en, original_title, aliases) "
                "VALUES ('orphan-id', 'x', '', '', '')"
            )
        )

    report = await fts.reconcile_fts(db_session)
    assert report == {"updated": 1, "deleted": 1}
    # Sidecar now mirrors the base table exactly.
    assert await _shadow_rows("tv_series_fts") == {series.id: "测试剧集"}


async def test_reconcile_swallows_shadow_write_errors(db_session, monkeypatch):
    db_session.add(_series())
    await db_session.flush()
    await fts.drain_fts_outbox(db_session)
    engine = fts._get_fts_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tv_series_fts "
                "(entity_id, title_cn, title_en, original_title, aliases) "
                "VALUES ('orphan-id', 'x', '', '', '')"
            )
        )

    async def _boom(*a, **kw):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(fts, "_shadow_write", _boom)
    # Divergence is still detected and counted even though the heal fails.
    report = await fts.reconcile_fts(db_session)
    assert report["deleted"] == 1


async def test_reconcile_non_turso_returns_zeros(db_session, monkeypatch):
    monkeypatch.setattr(fts, "_fts_available", lambda db: False)
    assert await fts.reconcile_fts(db_session) == {"updated": 0, "deleted": 0}
