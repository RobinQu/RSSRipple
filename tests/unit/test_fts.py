"""Tests for fts.py: Turso native FTS (ngram) upsert/search/delete/rebuild
for TVSeries, Movie, and AudioWork against a real Turso database.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.audio_work import AudioWork
from app.services.fts import (
    backfill_fts_if_empty,
    delete_audio_work_fts,
    delete_movie_fts,
    delete_series_fts,
    rebuild_audio_work_fts,
    rebuild_movie_fts,
    rebuild_series_fts,
    search_audio_work_fts,
    search_movie_fts,
    search_series_fts,
    upsert_audio_work_fts,
    upsert_movie_fts,
    upsert_series_fts,
)


def _audio_work(**kw) -> AudioWork:
    defaults = dict(
        title_cn="深夜音声作品",
        title_en="Late Night Audio",
        original_title="Late Night Audio",
        aliases=["音声别名"],
        external_source="manual",
        content_type="asmr",
    )
    defaults.update(kw)
    return AudioWork(**defaults)


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------


async def test_series_fts_upsert_search_delete(db_session, sample_series):
    await upsert_series_fts(db_session, sample_series)

    # ngram substring path (>= 2 chars after normalization)
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")
    # substring of the title also matches
    assert sample_series.id in await search_series_fts(db_session, "试剧")
    # English title also indexed (case-insensitive via normalization)
    assert sample_series.id in await search_series_fts(db_session, "test series")
    assert sample_series.id in await search_series_fts(db_session, "TEST SERIES")
    # Empty / whitespace query short-circuits
    assert await search_series_fts(db_session, "") == []
    assert await search_series_fts(db_session, "  ") == []

    await delete_series_fts(db_session, sample_series.id)
    # The base row still exists: the search_text LIKE fallback (which exists
    # precisely to heal index lag) still finds it.
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")
    # Deleting the base row removes it from search entirely.
    await db_session.delete(sample_series)
    await db_session.commit()
    assert await search_series_fts(db_session, "测试剧集") == []


async def test_series_fts_upsert_replaces_existing_row(db_session, sample_series):
    await upsert_series_fts(db_session, sample_series)
    sample_series.title_en = "Renamed Show"
    await upsert_series_fts(db_session, sample_series)

    assert sample_series.id in await search_series_fts(db_session, "renamed show")
    # Old row was deleted first — only one index entry for the entity
    hits = await search_series_fts(db_session, "test series")
    assert hits.count(sample_series.id) <= 1


async def test_rebuild_series_fts(db_session, sample_series):
    count = await rebuild_series_fts(db_session)
    assert count == 1
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")


# ---------------------------------------------------------------------------
# Movie
# ---------------------------------------------------------------------------


async def test_movie_fts_upsert_search_delete(db_session, sample_movie):
    await upsert_movie_fts(db_session, sample_movie)

    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")
    assert sample_movie.id in await search_movie_fts(db_session, "test movie")
    assert sample_movie.id in await search_movie_fts(db_session, "电影")
    assert await search_movie_fts(db_session, "") == []

    await delete_movie_fts(db_session, sample_movie.id)
    # Base row still present → the LIKE fallback still finds it (see the
    # series counterpart); deleting the base row removes it from search.
    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")
    await db_session.delete(sample_movie)
    await db_session.commit()
    assert await search_movie_fts(db_session, "测试电影") == []


async def test_rebuild_movie_fts(db_session, sample_movie):
    count = await rebuild_movie_fts(db_session)
    assert count == 1
    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")


# ---------------------------------------------------------------------------
# AudioWork
# ---------------------------------------------------------------------------


async def test_audio_work_fts_upsert_search_delete(db_session):
    aw = _audio_work()
    db_session.add(aw)
    await db_session.flush()

    await upsert_audio_work_fts(db_session, aw)
    assert aw.id in await search_audio_work_fts(db_session, "深夜音声作品")
    assert aw.id in await search_audio_work_fts(db_session, "late night audio")
    # Alias text is indexed too
    assert aw.id in await search_audio_work_fts(db_session, "别名")
    assert await search_audio_work_fts(db_session, "") == []

    await delete_audio_work_fts(db_session, aw.id)
    # Base row still present → the LIKE fallback still finds it; deleting the
    # base row removes it from search.
    assert aw.id in await search_audio_work_fts(db_session, "深夜音声作品")
    await db_session.delete(aw)
    await db_session.commit()
    assert await search_audio_work_fts(db_session, "深夜音声作品") == []


async def test_rebuild_audio_work_fts(db_session):
    aw = _audio_work()
    db_session.add(aw)
    await db_session.flush()

    count = await rebuild_audio_work_fts(db_session)
    assert count == 1
    assert aw.id in await search_audio_work_fts(db_session, "深夜音声作品")


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


async def test_backfill_fts_if_empty(db_session, sample_series, sample_movie):
    await backfill_fts_if_empty(db_session)
    assert sample_series.id in await search_series_fts(db_session, "测试剧集")
    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")


# ---------------------------------------------------------------------------
# Retrieval ranking (Python-side; Turso's fts_score returns 0 under pyturso 0.8.2)
# ---------------------------------------------------------------------------


async def test_search_fts_ranks_most_similar_first(db_session, sample_series):
    """``limit`` must cut by relevance, not by internal index order: the exact
    title beats longer/partial matches, and limit=1 returns it."""
    import uuid

    from app.models.series import TVSeries

    others = [
        TVSeries(
            id=str(uuid.uuid4()), title_cn="测试剧集 第二季",
            external_source="manual", content_type="tv",
        ),
        TVSeries(
            id=str(uuid.uuid4()), title_cn="测试剧集第二季加长版标题",
            external_source="manual", content_type="tv",
        ),
        # Non-contiguous ngram hits do NOT match (Tantivy ngram semantics).
        TVSeries(
            id=str(uuid.uuid4()), title_cn="剧集参考",
            external_source="manual", content_type="tv",
        ),
    ]
    db_session.add_all(others)
    await db_session.commit()

    hits = await search_series_fts(db_session, "测试剧集", limit=10)
    assert hits[0] == sample_series.id
    assert set(hits) == {sample_series.id, others[0].id, others[1].id}

    # limit cuts the tail by relevance, keeping the best matches.
    top = await search_series_fts(db_session, "测试剧集", limit=1)
    assert top == [sample_series.id]


async def test_search_fts_ranking_tie_breaks_deterministically(db_session):
    """Equal-similarity rows order by entity_id, so repeated searches are stable."""
    import uuid

    from app.models.series import TVSeries

    ids = sorted(str(uuid.uuid4()) for _ in range(2))
    rows = [
        TVSeries(id=i, title_cn="并列标题", external_source="manual", content_type="tv")
        for i in ids
    ]
    db_session.add_all(rows)
    await db_session.commit()

    hits = await search_series_fts(db_session, "并列标题", limit=10)
    assert hits == ids


# ---------------------------------------------------------------------------
# Error swallowing: FTS helpers must log-and-continue on DB failures
# ---------------------------------------------------------------------------


async def test_upsert_swallows_db_errors():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("no such table")
    entity = SimpleNamespace(
        id="x", title_cn="t", title_en=None, original_title=None, aliases=None
    )
    await upsert_series_fts(db, entity)
    await upsert_movie_fts(db, entity)
    await upsert_audio_work_fts(db, entity)


async def test_delete_swallows_db_errors():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("no such table")
    await delete_series_fts(db, "x")
    await delete_movie_fts(db, "x")
    await delete_audio_work_fts(db, "x")


async def test_search_swallows_db_errors():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("no such table")
    for search in (search_series_fts, search_movie_fts, search_audio_work_fts):
        assert await search(db, "long enough query") == []
        assert await search(db, "ab") == []


# ---------------------------------------------------------------------------
# PostgreSQL search path (mirrors the Turso ngram tokenizer semantics)
# ---------------------------------------------------------------------------


def test_escape_like_escapes_wildcards():
    from app.services.fts import _escape_like

    assert _escape_like("100%_ok\\x") == "100\\%\\_ok\\\\x"
    assert _escape_like("plain") == "plain"


def _capturing_db() -> tuple[object, dict]:
    """Return a fake async session that records the compiled SQL and returns [].

    ``_search_pg_like`` only touches ``db.execute(...).all()``, so a minimal
    stub suffices to assert on the generated query without a live backend.
    """
    captured: dict = {}

    class _Result:
        def all(self):
            return []

    class _Db:
        async def execute(self, stmt):
            captured["sql"] = str(
                stmt.compile(compile_kwargs={"literal_binds": True})
            )
            return _Result()

    return _Db(), captured


async def test_search_like_word_or_and_escaping():
    from app.models.series import TVSeries
    from app.services.fts import _search_like

    db, captured = _capturing_db()

    # Multi-word English query → one LIKE per ≥2-char token, OR-ed together.
    await _search_like(db, TVSeries, "ghost in the shell", 30)
    assert captured["sql"].count("LIKE") == 4
    assert "%ghost%" in captured["sql"] and "%shell%" in captured["sql"]

    # Single-token CJK query → single substring LIKE (contiguous match).
    await _search_like(db, TVSeries, "攻壳机动队", 30)
    assert captured["sql"].count("LIKE") == 1
    assert "%攻壳机动队%" in captured["sql"]

    # Single-character query → still matched as a substring.
    await _search_like(db, TVSeries, "测", 30)
    assert captured["sql"].count("LIKE") == 1
    assert "%测%" in captured["sql"]

    # LIKE wildcards in the query term are escaped, so "100%" matches literally.
    await _search_like(db, TVSeries, "100%", 30)
    assert "%100\\%%" in captured["sql"]

    # 1-char tokens in a multi-word query are dropped (ngram min_token_size=2).
    captured.clear()
    await _search_like(db, TVSeries, "a b", 30)
    assert "sql" not in captured


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


async def test_reconcile_fts_heals_divergence(db_session, sample_series, sample_movie):
    from sqlalchemy import text

    from app.services.fts import _get_fts_engine, reconcile_fts

    await backfill_fts_if_empty(db_session)

    engine = _get_fts_engine()
    async with engine.begin() as conn:
        # 1. stale content on an existing shadow row
        await conn.execute(text(
            "UPDATE tv_series_fts SET title_en = 'stale title' WHERE entity_id = :id"
        ), {"id": sample_series.id})
        # 2. orphan shadow row (base row does not exist)
        await conn.execute(text(
            "INSERT INTO tv_series_fts (entity_id, title_cn) VALUES ('orphan-id', '幽灵')"
        ))
        # 3. missing shadow row (movie deleted from shadow)
        await conn.execute(text(
            "DELETE FROM movie_fts WHERE entity_id = :id"
        ), {"id": sample_movie.id})

    report = await reconcile_fts(db_session)
    assert report["updated"] == 2  # stale series row + missing movie row
    assert report["deleted"] == 1  # orphan

    assert sample_series.id in await search_series_fts(db_session, "test series")
    assert sample_series.id not in await search_series_fts(db_session, "stale")
    assert sample_movie.id in await search_movie_fts(db_session, "测试电影")
    assert "orphan-id" not in await search_series_fts(db_session, "幽灵")


async def test_reconcile_fts_noop_when_in_sync(db_session, sample_series):
    from app.services.fts import reconcile_fts

    await backfill_fts_if_empty(db_session)
    report = await reconcile_fts(db_session)
    assert report == {"updated": 0, "deleted": 0}


async def test_reconcile_loss_stops_after_first_shadow_table(db_session, sample_series, sample_movie, monkeypatch):
    import pytest
    from sqlalchemy import text

    from app.services import fts, scheduler, task_queue

    await db_session.commit()
    await backfill_fts_if_empty(db_session)
    engine = fts._get_fts_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM tv_series_fts"))
        await conn.execute(text("DELETE FROM movie_fts"))
    expired = False
    writes = []
    write = fts._shadow_write

    async def guard():
        if expired:
            raise task_queue.ExecutionOwnershipLostError("Expired after first table")

    async def write_then_expire(statements):
        nonlocal expired
        await write(statements)
        writes.append(statements)
        expired = True

    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    monkeypatch.setattr(fts, "_shadow_write", write_then_expire)
    with pytest.raises(task_queue.ExecutionOwnershipLostError):
        await scheduler._reconcile_fts()
    assert len(writes) == 1
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM tv_series_fts")) == 1
        assert await conn.scalar(text("SELECT count(*) FROM movie_fts")) == 0


# ---------------------------------------------------------------------------
# Sidecar engine bootstrap, URL derivation, availability detection
# ---------------------------------------------------------------------------


class _RaisingExecCtx:
    """Async context manager whose execute() always raises — simulates a
    broken/unreachable sidecar engine."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **kw):
        raise RuntimeError("sidecar boom")


class _FakeEngine:
    """Engine-shaped object whose begin() context raises on execute."""

    def begin(self):
        return _RaisingExecCtx()


class _FakeConnectEngine:
    """Engine-shaped object whose connect() context raises on execute."""

    def connect(self):
        return _RaisingExecCtx()


class _EmptyScalars:
    def scalars(self):
        return self

    def all(self):
        return []


def _fts_available_db() -> SimpleNamespace:
    """A fake session exposing a turso engine URL so _fts_available() is True."""
    return SimpleNamespace(
        engine=SimpleNamespace(url="sqlite+aioturso:///test.db"),
        execute=AsyncMock(return_value=_EmptyScalars()),
    )


async def test_sidecar_url_derivation(monkeypatch):
    import app.services.fts as fts_mod

    monkeypatch.setattr(fts_mod.settings, "database_url", "sqlite+aioturso:///:memory:")
    assert fts_mod._sidecar_url().startswith(
        "sqlite+aioturso:///data/rss_ripple_fts_fts.db"
    )

    monkeypatch.setattr(fts_mod.settings, "database_url", "sqlite+aioturso:///data/rss_ripple_turso.db")
    assert fts_mod._sidecar_url() == (
        "sqlite+aioturso:///data/rss_ripple_turso_fts.db"
        "?experimental_features=index_method"
    )

    # Query string stripped and extensionless paths get a plain suffix.
    monkeypatch.setattr(
        fts_mod.settings, "database_url",
        "sqlite+aioturso:////var/lib/rssripple/data/shows?mode=ro",
    )
    assert fts_mod._sidecar_url() == (
        "sqlite+aioturso:////var/lib/rssripple/data/shows_fts.db"
        "?experimental_features=index_method"
    )


async def test_get_fts_engine_bootstraps_when_none(monkeypatch):
    import app.services.fts as fts_mod

    monkeypatch.setattr(fts_mod, "_FTS_ENGINE", None)
    engine = fts_mod._get_fts_engine()
    assert engine is not None
    await engine.dispose()


async def test_fts_available_from_engine_url():
    from app.services.fts import _fts_available

    assert _fts_available(SimpleNamespace(engine=SimpleNamespace(url="sqlite+aioturso:///x"))) is True
    assert _fts_available(SimpleNamespace(engine=SimpleNamespace(url="postgresql+asyncpg:///x"))) is False


async def test_fts_available_from_sync_session_bind():
    from app.services.fts import _fts_available

    bind = SimpleNamespace(url="sqlite+aioturso:///x")
    db = SimpleNamespace(sync_session=SimpleNamespace(get_bind=lambda: bind))
    assert _fts_available(db) is True


async def test_fts_available_falls_back_to_db_object_url():
    from app.services.fts import _fts_available

    assert _fts_available(SimpleNamespace(url="sqlite+aioturso:///x")) is True
    assert _fts_available(SimpleNamespace(url="postgresql+asyncpg:///x")) is False


async def test_ensure_fts_tables_skips_non_turso(monkeypatch):
    import app.services.fts as fts_mod

    monkeypatch.setattr(fts_mod, "_FTS_ENGINE", None)
    monkeypatch.setattr(fts_mod.settings, "database_url", "postgresql+asyncpg:///x")
    await fts_mod.ensure_fts_tables()


async def test_ensure_fts_tables_logs_creation_failures(monkeypatch):
    import app.services.fts as fts_mod

    monkeypatch.setattr(fts_mod, "_FTS_ENGINE", object())
    monkeypatch.setattr(fts_mod, "_get_fts_engine", lambda: _FakeEngine())
    await fts_mod.ensure_fts_tables()


# ---------------------------------------------------------------------------
# Fallback / error-swallowing on the FTS sidecar
# ---------------------------------------------------------------------------


async def test_search_like_swallows_db_errors():
    from app.services.fts import search_series_fts

    db = SimpleNamespace(
        engine=SimpleNamespace(url="sqlite+aioturso:///x"),
        execute=AsyncMock(side_effect=RuntimeError("boom")),
    )
    assert await search_series_fts(db, "测") == []


async def test_search_like_honors_limit(db_session, sample_series):
    import uuid

    from app.models.series import TVSeries

    extras = [
        TVSeries(
            id=str(uuid.uuid4()), title_cn=f"测剧{n}", title_en=f"UniqueZ{n}",
            external_source="manual", content_type="tv",
        )
        for n in range(3)
    ]
    db_session.add_all(extras)
    await db_session.commit()

    ids = await search_series_fts(db_session, "测", limit=2)
    assert len(ids) == 2
    assert all(i in {sample_series.id, *(e.id for e in extras)} for i in ids)


async def test_search_like_ranks_most_similar_first(db_session, sample_series):
    """The LIKE path (PG primary / Turso recall fallback) returns the most
    similar candidate first and ``limit`` cuts by relevance."""
    import uuid

    from app.models.series import TVSeries

    longer = TVSeries(
        id=str(uuid.uuid4()), title_cn="测试剧集 第二季 特别长的标题后缀",
        external_source="manual", content_type="tv",
    )
    db_session.add(longer)
    await db_session.commit()

    from app.services.fts import _search_like

    ids = await _search_like(db_session, TVSeries, "测试剧集", 10)
    assert ids[0] == sample_series.id
    assert longer.id in ids
    top = await _search_like(db_session, TVSeries, "测试剧集", 1)
    assert top == [sample_series.id]


async def test_empty_fts_recall_falls_back_to_search_text_like(db_session, monkeypatch):
    """When fts_match returns nothing (separator punctuation breaks ngram
    contiguity, or a lagging sidecar), search falls back to the bounded
    search_text LIKE pre-filter instead of a full-table scan."""
    import uuid

    import app.services.fts as fts_mod
    from app.models.series import TVSeries

    work = TVSeries(
        id=str(uuid.uuid4()), title_en="Initial D: Battle Stage",
        external_source="manual", content_type="tv",
    )
    db_session.add(work)
    await db_session.commit()

    # Simulate an empty FTS recall; the LIKE fallback must still find the row.
    monkeypatch.setattr(fts_mod, "_search_fts", AsyncMock(return_value=[]))
    ids = await search_series_fts(db_session, "initial d battle stage")
    assert work.id in ids


async def test_empty_fts_recall_fallback_also_bounded(db_session, monkeypatch):
    """The LIKE fallback returns empty (not a full-table rescue) when nothing
    substring-matches either."""
    import app.services.fts as fts_mod

    monkeypatch.setattr(fts_mod, "_search_fts", AsyncMock(return_value=[]))
    assert await search_series_fts(db_session, "不存在的作品标题") == []


async def test_upsert_delete_swallow_sidecar_write_errors(monkeypatch):
    import app.services.fts as fts_mod

    entity = SimpleNamespace(
        id="e1", title_cn="标题", title_en="Title", original_title=None, aliases=[]
    )
    db = _fts_available_db()
    monkeypatch.setattr(fts_mod, "_get_fts_engine", lambda: _FakeEngine())

    await fts_mod.upsert_series_fts(db, entity)
    await fts_mod.upsert_movie_fts(db, entity)
    await fts_mod.upsert_audio_work_fts(db, entity)
    await fts_mod.delete_series_fts(db, "e1")
    await fts_mod.delete_movie_fts(db, "e1")
    await fts_mod.delete_audio_work_fts(db, "e1")


async def test_search_fts_swallows_sidecar_query_errors(monkeypatch):
    import app.services.fts as fts_mod

    db = _fts_available_db()
    monkeypatch.setattr(fts_mod, "_get_fts_engine", lambda: _FakeConnectEngine())

    assert await fts_mod.search_series_fts(db, "测试剧集") == []
    assert await fts_mod.search_movie_fts(db, "测试电影") == []
    assert await fts_mod.search_audio_work_fts(db, "深夜音声") == []


async def test_rebuild_skips_when_fts_unavailable():
    from app.services.fts import rebuild_audio_work_fts, rebuild_movie_fts, rebuild_series_fts

    db = SimpleNamespace(
        engine=SimpleNamespace(url="postgresql+asyncpg:///x"), execute=AsyncMock()
    )
    assert await rebuild_series_fts(db) == 0
    assert await rebuild_movie_fts(db) == 0
    assert await rebuild_audio_work_fts(db) == 0


async def test_rebuild_swallows_sidecar_write_errors(monkeypatch):
    import app.services.fts as fts_mod

    entity = SimpleNamespace(
        id="e1", title_cn="标题", title_en="Title", original_title=None, aliases=[]
    )
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [entity]))
    db = SimpleNamespace(
        engine=SimpleNamespace(url="sqlite+aioturso:///x"),
        execute=AsyncMock(return_value=result),
    )
    monkeypatch.setattr(fts_mod, "_get_fts_engine", lambda: _FakeEngine())

    assert await fts_mod.rebuild_series_fts(db) == 0
    assert await fts_mod.rebuild_movie_fts(db) == 0
    assert await fts_mod.rebuild_audio_work_fts(db) == 0


async def test_backfill_fts_skips_when_unavailable():
    from app.services.fts import backfill_fts_if_empty

    db = SimpleNamespace(engine=SimpleNamespace(url="postgresql+asyncpg:///x"))
    await backfill_fts_if_empty(db)


async def test_backfill_fts_continues_on_count_query_error(db_session, monkeypatch):
    import app.services.fts as fts_mod

    monkeypatch.setattr(fts_mod, "_get_fts_engine", lambda: _FakeConnectEngine())
    await fts_mod.backfill_fts_if_empty(db_session)


async def test_drain_noop_on_non_turso(db_session, monkeypatch):
    import app.services.fts as fts_mod

    monkeypatch.setattr(fts_mod.settings, "database_url", "postgresql+asyncpg:///x")
    assert await fts_mod.drain_fts_outbox(db_session) == 0


async def test_drain_write_failure_logs_and_consumes_outbox(db_session, sample_series, monkeypatch):
    import app.services.fts as fts_mod

    await db_session.commit()
    monkeypatch.setattr(fts_mod, "_get_fts_engine", lambda: _FakeEngine())
    assert await fts_mod.drain_fts_outbox(db_session) == 1
    assert await fts_mod.drain_fts_outbox(db_session) == 0


async def test_reconcile_skips_when_unavailable():
    from app.services.fts import reconcile_fts

    db = SimpleNamespace(engine=SimpleNamespace(url="postgresql+asyncpg:///x"))
    assert await reconcile_fts(db) == {"updated": 0, "deleted": 0}


async def test_reconcile_fts_logs_write_failure(db_session, sample_series, monkeypatch):
    from sqlalchemy import text

    import app.services.fts as fts_mod

    await db_session.commit()
    await fts_mod.drain_fts_outbox(db_session)
    await db_session.commit()

    engine = fts_mod._get_fts_engine()
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE tv_series_fts SET title_en = 'stale' WHERE entity_id = :id"
        ), {"id": sample_series.id})

    monkeypatch.setattr(fts_mod, "_shadow_write", AsyncMock(side_effect=RuntimeError("nope")))
    report = await fts_mod.reconcile_fts(db_session)
    assert report["updated"] >= 1


# ---------------------------------------------------------------------------
# Single-char fallback and normalization edge cases (movie / audio / NFKC)
# ---------------------------------------------------------------------------


async def test_movie_single_char_fallback(db_session, sample_movie):
    await db_session.commit()
    assert sample_movie.id in await search_movie_fts(db_session, "电")


async def test_audio_work_single_char_fallback(db_session):
    aw = _audio_work(title_cn="音声作品")
    db_session.add(aw)
    await db_session.commit()
    assert aw.id in await search_audio_work_fts(db_session, "音")


async def test_search_fullwidth_query_normalization(db_session, sample_series):
    await db_session.commit()
    await upsert_series_fts(db_session, sample_series)
    # Full-width letters are NFKC-folded to ASCII and lowercased before search.
    assert sample_series.id in await search_series_fts(db_session, "ＴＥＳＴ ＳＥＲＩＥＳ")


async def test_pre_search_drain_failure_preserves_caller_transaction(db_session, sample_series, monkeypatch):
    from sqlalchemy import select, text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.series import TVSeries
    from app.services import fts

    await db_session.commit()
    original_title = sample_series.title_en
    series_id = sample_series.id
    await db_session.scalar(select(TVSeries.id).where(TVSeries.id == series_id))
    sample_series.title_en = "uncommitted caller title"

    async def connection_failure(session):
        await session.execute(text("SELECT 1"))
        await (await session.connection()).invalidate()
        raise OperationalError("DELETE FROM fts_outbox", {}, Exception("database is locked"))

    monkeypatch.setattr(fts, "drain_fts_outbox", connection_failure)
    await fts._drain_pending_changes(db_session)
    assert await db_session.scalar(select(TVSeries.title_en).where(TVSeries.id == series_id)) == "uncommitted caller title"
    async with AsyncSession(bind=db_session.bind) as other:
        assert await other.scalar(select(TVSeries.title_en).where(TVSeries.id == series_id)) == original_title
