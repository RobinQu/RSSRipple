"""Unit tests for ``_apply_light_migrations`` channel metadata-source
convergence plus the database setup helpers in ``app/database.py``."""

import contextlib
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database import (
    _DB_RETRY_BASE_S,
    _MAX_DB_RETRIES,
    Base,
    _apply_light_migrations,
    _backoff_delay,
    _best_effort,
    _is_lock_timeout,
    _is_retryable_lock_error,
    apply_db_pragmas,
    committed_session,
    get_db,
    install_db_retry_middleware,
    normalize_database_url,
    retry_on_lock,
)
from app.models.channel import Channel


def _db_error(msg: str) -> DatabaseError:
    return DatabaseError("SELECT 1", {}, Exception(msg))


@contextlib.asynccontextmanager
async def _raw_turso_engine():
    """Fresh file-backed Turso engine with the full current schema.

    Uses a plain (non-CONCURRENT) URL so raw DDL rebuilds work, and leaves
    foreign-key enforcement off so old-shape tables can be fabricated without
    satisfying every cross-table reference.
    """
    import tempfile
    from pathlib import Path

    from sqlalchemy.ext.asyncio import create_async_engine

    td = tempfile.TemporaryDirectory(prefix="rssripple-raw-")
    url = f"sqlite+aioturso:///{Path(td.name)}/raw.db"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        td.cleanup()


def _channel(name: str, metadata_source: str | None) -> Channel:
    return Channel(
        name=name,
        url=f"https://example.com/{name}.xml",
        field_mapping={"field_mappings": {"title_cn": {"source": "title"}}},
        metadata_source=metadata_source,
    )


async def test_metadata_source_convergence_rewrites_legacy_values(db_engine, db_session):
    db_session.add_all([
        _channel("exa", "exa"),
        _channel("jina", "jina"),
        _channel("local", "local"),
        _channel("combined", "combined"),
        _channel("wiki", "wikipedia"),
        _channel("tmdb", "tmdb"),
        _channel("bangumi", "bangumi"),
        _channel("unset", None),
    ])
    await db_session.commit()

    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)

    rows = (await db_session.execute(select(Channel.name, Channel.metadata_source))).all()
    by_name = dict(rows)
    # Legacy channel sources converge on wikipedia; valid values untouched;
    # NULL stays NULL (resolves to the default at runtime).
    assert by_name["exa"] == "wikipedia"
    assert by_name["jina"] == "wikipedia"
    assert by_name["local"] == "wikipedia"
    assert by_name["combined"] == "wikipedia"
    assert by_name["wiki"] == "wikipedia"
    assert by_name["tmdb"] == "tmdb"
    assert by_name["bangumi"] == "bangumi"
    assert by_name["unset"] is None


async def test_metadata_fallback_sources_column_is_added(db_engine, db_session):
    """The migration adds the JSON whitelist column; a stored list round-trips."""
    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)
        cols = (await conn.execute(text("PRAGMA table_info(channels)"))).fetchall()
        assert "metadata_fallback_sources" in {row[1] for row in cols}

    ch = _channel("wl", "wikipedia")
    ch.metadata_fallback_sources = ["bangumi", "mal"]
    db_session.add(ch)
    await db_session.commit()
    await db_session.refresh(ch)
    assert ch.metadata_fallback_sources == ["bangumi", "mal"]


async def test_migrations_are_idempotent(db_engine, db_session):
    db_session.add(_channel("exa", "exa"))
    await db_session.commit()
    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)
        await _apply_light_migrations(conn)  # second run must be a no-op
    row = (await db_session.execute(
        select(Channel.metadata_source).where(Channel.name == "exa")
    )).scalar_one()
    assert row == "wikipedia"


async def test_legacy_required_titles_are_unlocked_once(db_engine, db_session):
    """Old baseline title fields stop gating resources, but later explicit
    opt-in survives subsequent startups."""
    ch = _channel("required-titles", "wikipedia")
    ch.required_metadata_fields = ["title_cn", "title_en", "search_title"]
    db_session.add(ch)
    await db_session.commit()

    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)
    await db_session.refresh(ch)
    assert "title_cn" not in ch.required_metadata_fields
    assert "title_en" not in ch.required_metadata_fields

    ch.required_metadata_fields = [*ch.required_metadata_fields, "title_cn"]
    await db_session.commit()
    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)
    await db_session.refresh(ch)
    assert "title_cn" in ch.required_metadata_fields


async def test_torrent_detection_columns_are_added(db_engine, db_session):
    """Torrent content detection P1: batch_scope / collection_id / torrent_file
    on file_resources — migration is idempotent and values round-trip."""
    from app.models.file_resource import FileResource

    for _ in range(2):  # idempotent
        async with db_engine.begin() as conn:
            await _apply_light_migrations(conn)
            cols = (await conn.execute(text("PRAGMA table_info(file_resources)"))).fetchall()
            names = {row[1] for row in cols}
            assert {"batch_scope", "collection_id", "torrent_file"} <= names

    ch = _channel("det", "wikipedia")
    db_session.add(ch)
    await db_session.flush()
    res = FileResource(
        channel_id=ch.id,
        guid="g1",
        title_raw="[Group] Franchise Pack",
        torrent_url="magnet:?xt=urn:btih:xyz",
        batch_scope="franchise",
        torrent_file="cache/torrents/xyz.torrent",
    )
    db_session.add(res)
    await db_session.commit()
    await db_session.refresh(res)
    assert res.batch_scope == "franchise"
    assert res.collection_id is None
    assert res.torrent_file == "cache/torrents/xyz.torrent"


async def test_plex_env_migration_creates_media_server(db_engine, db_session, monkeypatch):
    """存量全局 PLEX_URL/PLEX_TOKEN 环境变量 → 一条 Plex MediaServerInstance；
    libraries.plex_section 值拷到 section_key。幂等：实例表非空不再插。"""
    from app.models.library import Library
    from app.models.media_server import MediaServerInstance

    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "tok")
    lib = Library(name="Movies", kind="movie", plex_section="3")
    db_session.add(lib)
    await db_session.commit()

    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)
        await _apply_light_migrations(conn)  # 幂等：第二次不再插

    servers = (await db_session.execute(select(MediaServerInstance))).scalars().all()
    assert len(servers) == 1
    assert servers[0].type == "plex" and servers[0].url == "http://plex:32400"
    assert servers[0].token == "tok" and servers[0].enabled is True
    await db_session.refresh(lib)
    assert lib.section_key == "3"  # plex_section → section_key


async def test_plex_env_migration_skipped_without_env(db_engine, db_session, monkeypatch):
    """无 PLEX_* 环境变量 → 不插实例。"""
    from app.models.media_server import MediaServerInstance

    monkeypatch.delenv("PLEX_URL", raising=False)
    monkeypatch.delenv("PLEX_TOKEN", raising=False)
    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)
    servers = (await db_session.execute(select(MediaServerInstance))).scalars().all()
    assert servers == []


async def test_stale_ambiguous_cleanup(db_engine, db_session):
    """One-time healing: ambiguous flags stuck on resources that carry no
    episode/season question (合集 / movie-linked / non-tv work) are cleared;
    genuinely ambiguous tv episodes are untouched. Idempotent via the
    app_settings sentinel."""
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.series import TVSeries

    ch = _channel("ambig", "wikipedia")
    movie = Movie(title_en="M", content_type="movie")
    movie_typed_series = TVSeries(title_en="MS", content_type="movie")
    tv_series = TVSeries(title_en="TS", content_type="tv")
    db_session.add_all([ch, movie, movie_typed_series, tv_series])
    await db_session.flush()

    def _res(guid, **kw):
        return FileResource(
            channel_id=ch.id, guid=guid, title_raw=f"[G] {guid}",
            torrent_url=f"magnet:?xt=urn:btih:{guid}", **kw,
        )

    db_session.add_all([
        # 合集 + ambiguous → "manual" (a human made the batch call)
        _res("b1", series_id=tv_series.id, is_batch=True, batch_scope="season",
             season=1, episode_confidence="ambiguous"),
        # movie-linked + ambiguous → NULL
        _res("m1", movie_id=movie.id, episode_confidence="ambiguous"),
        # linked to a work reclassified away from tv → NULL
        _res("s1", series_id=movie_typed_series.id, episode_confidence="ambiguous"),
        # genuine tv episode question → untouched
        _res("t1", series_id=tv_series.id, episode=200, episode_confidence="ambiguous"),
    ])
    await db_session.commit()

    for _ in range(2):  # second run must be a no-op (sentinel = done)
        async with db_engine.begin() as conn:
            await _apply_light_migrations(conn)

    rows = (await db_session.execute(
        select(FileResource.guid, FileResource.episode_confidence)
    )).all()
    by_guid = dict(rows)
    assert by_guid["b1"] == "manual"
    assert by_guid["m1"] is None
    assert by_guid["s1"] is None
    assert by_guid["t1"] == "ambiguous"


async def test_per_season_work_columns_are_added(db_engine, db_session):
    """作品单季化 P2: tv_series.season_number + work_collections
    aliases/search_text/manually_edited_fields — idempotent, values
    round-trip, and the ORM/server default lands on existing-shaped rows."""
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection

    for _ in range(2):  # idempotent
        async with db_engine.begin() as conn:
            await _apply_light_migrations(conn)
            cols = (await conn.execute(text("PRAGMA table_info(tv_series)"))).fetchall()
            assert "season_number" in {row[1] for row in cols}
            cols = (await conn.execute(text("PRAGMA table_info(work_collections)"))).fetchall()
            assert {"aliases", "search_text", "manually_edited_fields"} <= {
                row[1] for row in cols
            }

    coll = WorkCollection(title_cn="某IP", aliases=["别名甲"])
    s = TVSeries(title_cn="某IP 第一季", content_type="tv")
    db_session.add_all([coll, s])
    await db_session.commit()
    await db_session.refresh(coll)
    await db_session.refresh(s)
    assert s.season_number == 1  # default, never NULL
    assert coll.aliases == ["别名甲"]
    assert coll.manually_edited_fields is None


# ---------------------------------------------------------------------------
# 数据库设置助手（engine/factory/pragma/retry 等）
# ---------------------------------------------------------------------------


def test_is_turso_url():
    from app.database import is_turso_url

    assert is_turso_url("sqlite+aioturso:///x.db")
    assert not is_turso_url("postgresql+asyncpg://host/db")


def test_normalize_database_url():
    assert normalize_database_url("sqlite+aioturso:///x.db") == \
        "sqlite+aioturso:///x.db?isolation_level=CONCURRENT"
    assert normalize_database_url("sqlite+aioturso:///x.db?mode=rw") == \
        "sqlite+aioturso:///x.db?mode=rw&isolation_level=CONCURRENT"
    assert normalize_database_url("sqlite+aioturso:///x.db?isolation_level=CONCURRENT") == \
        "sqlite+aioturso:///x.db?isolation_level=CONCURRENT"
    assert normalize_database_url("postgresql+asyncpg://h/db") == \
        "postgresql+asyncpg://h/db"


def test_is_retryable_lock_error():
    assert _is_retryable_lock_error(_db_error("database is locked"))
    assert _is_retryable_lock_error(_db_error("Write-write conflict detected"))
    assert not _is_retryable_lock_error(_db_error("connection refused"))
    assert not _is_retryable_lock_error(ValueError("database is locked"))


def test_backoff_delay_grows():
    assert _backoff_delay(0) >= _DB_RETRY_BASE_S
    assert _backoff_delay(3) > _backoff_delay(2)


async def test_retry_on_lock_succeeds_after_transient_failures():
    calls = 0

    async def _op():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _db_error("database is locked")
        return "ok"

    assert await retry_on_lock(_op) == "ok"
    assert calls == 3


async def test_retry_on_lock_reraises_non_retryable():
    async def _op():
        raise _db_error("connection refused")

    with pytest.raises(DatabaseError):
        await retry_on_lock(_op)


async def test_retry_on_lock_reraises_when_exhausted():
    async def _op():
        raise _db_error("write-write conflict")

    with pytest.raises(DatabaseError):
        await retry_on_lock(_op)


async def test_retry_on_lock_success_on_first_attempt():
    async def _op():
        return 42

    assert await retry_on_lock(_op) == 42


async def test_committed_session_commits_on_normal_exit(db_engine, db_session):
    from app.models.agent import Agent
    from app.models.downloader import DownloaderInstance

    dl = DownloaderInstance(
        id=str(uuid.uuid4()), name="D", type="transmission",
        url="http://x", download_dir="/dl", status="disconnected",
    )
    db_session.add(dl)
    ch = Channel(name="cmt", url="https://x", field_mapping={})
    db_session.add(ch)
    await db_session.commit()

    async with committed_session() as session:
        session.add(Agent(
            id=str(uuid.uuid4()), name="A", channel_id=ch.id,
            downloader_id=dl.id, conflict_resolution="auto",
        ))
    count = (await db_session.execute(select(func.count()).select_from(Agent))).scalar_one()
    assert count == 1


async def test_committed_session_rolls_back_on_error(db_engine, db_session):
    from app.models.agent import Agent
    from app.models.downloader import DownloaderInstance

    dl = DownloaderInstance(
        id=str(uuid.uuid4()), name="D", type="transmission",
        url="http://x", download_dir="/dl", status="disconnected",
    )
    db_session.add(dl)
    ch = Channel(name="cmt", url="https://x", field_mapping={})
    db_session.add(ch)
    await db_session.commit()

    with pytest.raises(RuntimeError, match="boom"):
        async with committed_session() as session:
            session.add(Agent(
                id=str(uuid.uuid4()), name="A", channel_id=ch.id,
                downloader_id=dl.id, conflict_resolution="auto",
            ))
            raise RuntimeError("boom")
    count = (await db_session.execute(select(func.count()).select_from(Agent))).scalar_one()
    assert count == 0


async def test_get_db_commits_and_rolls_back(db_engine, db_session):
    from app.models.agent import Agent
    from app.models.downloader import DownloaderInstance

    ch = Channel(name="gdb", url="https://x", field_mapping={})
    dl = DownloaderInstance(
        id=str(uuid.uuid4()), name="D", type="transmission",
        url="http://x", download_dir="/dl", status="disconnected",
    )
    db_session.add_all([ch, dl])
    await db_session.commit()

    gen = get_db()
    session = await anext(gen)
    session.add(Agent(
        id=str(uuid.uuid4()), name="A", channel_id=ch.id,
        downloader_id=dl.id, conflict_resolution="auto",
    ))
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    count = (await db_session.execute(select(func.count()).select_from(Agent))).scalar_one()
    assert count == 1

    gen2 = get_db()
    session2 = await anext(gen2)
    session2.add(Agent(
        id=str(uuid.uuid4()), name="B", channel_id=ch.id,
        downloader_id=dl.id, conflict_resolution="auto",
    ))
    with pytest.raises(RuntimeError, match="boom"):
        await gen2.athrow(RuntimeError("boom"))
    count = (await db_session.execute(select(func.count()).select_from(Agent))).scalar_one()
    assert count == 1


def test_install_db_retry_middleware_noop_for_postgres(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://host/db")
    app = object()
    assert install_db_retry_middleware(app) is app


def test_install_db_retry_middleware_turso_retries():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient


    app = FastAPI()
    state = {"calls": 0}

    @app.get("/boom")
    async def boom():
        state["calls"] += 1
        if state["calls"] < 3:
            raise _db_error("database is locked")
        return {"ok": True}

    install_db_retry_middleware(app)
    client = TestClient(app)
    resp = client.get("/boom")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert state["calls"] == 3


def test_install_db_retry_middleware_turso_gives_up():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    state = {"calls": 0}

    @app.get("/boom")
    async def boom():
        state["calls"] += 1
        raise _db_error("database is locked")

    install_db_retry_middleware(app)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert state["calls"] == _MAX_DB_RETRIES


def test_apply_db_pragmas_non_turso_is_noop():
    from types import SimpleNamespace

    apply_db_pragmas(SimpleNamespace(url="postgresql+asyncpg://host/db"))


def test_is_lock_timeout():
    from types import SimpleNamespace

    exc = SimpleNamespace(orig=SimpleNamespace(sqlstate="55P03"))
    assert _is_lock_timeout(exc) is True
    assert _is_lock_timeout(SimpleNamespace(orig=SimpleNamespace(sqlstate="42P01"))) is False
    assert _is_lock_timeout(SimpleNamespace()) is False


async def test_best_effort_success_and_swallow(db_engine):
    async with db_engine.begin() as conn:
        async with _best_effort(conn, "good"):
            await conn.execute(text("SELECT 1"))
        async with _best_effort(conn, "bad"):
            await conn.execute(text("SELECT * FROM missing_table_xyz"))
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


async def test_best_effort_savepoint_unavailable():
    class _Conn:
        def __init__(self):
            self.calls = []

        async def begin_nested(self):
            raise RuntimeError("no savepoint")

    conn = _Conn()
    ran = []
    async with _best_effort(conn, "unavailable"):
        ran.append(1)
        raise RuntimeError("body boom")
    assert ran == [1]


async def test_create_tables_turso_path(db_engine):
    from app.database import create_tables

    await create_tables()
    async with db_engine.begin() as conn:
        cols = (await conn.execute(text("PRAGMA table_info(tv_series)"))).fetchall()
        assert "season_number" in {row[1] for row in cols}


async def test_create_tables_legacy_non_turso_path(db_engine, monkeypatch):
    from app.config import settings
    from app.database import create_tables

    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///legacy.db")
    await create_tables()


# ---------------------------------------------------------------------------
# 轻迁移：存量数据驱动的分支（Turso）
# ---------------------------------------------------------------------------


async def test_light_migrations_column_additions_alter():
    """旧库缺列 → ALTER TABLE 补列（覆盖新增列的日志分支）。"""
    async with _raw_turso_engine() as engine:
        async with engine.begin() as conn:
            await conn.execute(text(
                "ALTER TABLE file_resources DROP COLUMN episode_confidence"
            ))
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)
            cols = (await conn.execute(text("PRAGMA table_info(file_resources)"))).fetchall()
            assert "episode_confidence" in {row[1] for row in cols}


async def test_light_migrations_agent_webhook_copy():
    """存量 agents.notify_webhook_* 列 → agent_webhooks 行拷贝（幂等）。"""
    async with _raw_turso_engine() as engine:
        async with engine.begin() as conn:
            await conn.execute(text(
                "ALTER TABLE agents ADD COLUMN notify_webhook_url VARCHAR(1024)"
            ))
            await conn.execute(text(
                "ALTER TABLE agents ADD COLUMN notify_webhook_mock BOOLEAN"
            ))
            await conn.execute(text(
                "INSERT INTO agents (id, name, channel_id, downloader_id, "
                "conflict_resolution, task_expire_days, llm_enabled, scope_channel_wide, "
                "status, notify_webhook_url, notify_webhook_mock) "
                "VALUES ('a1', 'A', 'c1', 'd1', 'auto', 30, 1, 0, 'active', "
                "'http://hook/x', 1)"
            ))
            await conn.execute(text(
                "INSERT INTO agents (id, name, channel_id, downloader_id, "
                "conflict_resolution, task_expire_days, llm_enabled, scope_channel_wide, "
                "status, notify_webhook_url, notify_webhook_mock) "
                "VALUES ('a2', 'B', 'c1', 'd1', 'auto', 30, 1, 0, 'active', "
                "'http://hook/y', 0)"
            ))
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)
            rows = (await conn.execute(text(
                "SELECT agent_id, url, mock, enabled FROM agent_webhooks"
            ))).fetchall()
            assert {(r[0], r[1], bool(r[2])) for r in rows} == {
                ("a1", "http://hook/x", True), ("a2", "http://hook/y", False),
            }
            await _apply_light_migrations(conn)  # 幂等：已有行跳过
            rows = (await conn.execute(text(
                "SELECT COUNT(*) FROM agent_webhooks"
            ))).scalar_one()
            assert rows == 2


async def test_light_migrations_required_fields_null_and_bad_json():
    """NULL 与非法 JSON 的 required_metadata_fields 收敛到基线。"""
    from app.services.required_fields import normalize_required_fields

    baseline = normalize_required_fields([])
    async with _raw_turso_engine() as engine:
        async with engine.begin() as conn:
            _CH = ("INSERT INTO channels (id, name, type, url, fetch_interval, "
                    "status, field_mapping, metadata_agent_enabled, default_is_anime, "
                    "auto_cleanup_unresolved_enabled, auto_cleanup_unresolved_days, "
                    "metadata_refresh_enabled, metadata_refresh_full_scope, metadata_source) "
                    "VALUES ('{id}', '{name}', 'rss_feed', 'https://{name}.x', 1800, "
                    "'active', '{{}}', 1, 0, 0, 21, 0, 0, 'wikipedia')")
            await conn.execute(text(_CH.format(id="c1", name="null-rf")))
            await conn.execute(text(
                "UPDATE channels SET required_metadata_fields = NULL WHERE id = 'c1'"
            ))
            await conn.execute(text(_CH.format(id="c2", name="bad-json")))
            await conn.execute(text(
                "UPDATE channels SET required_metadata_fields = 'not-json' WHERE id = 'c2'"
            ))
            await conn.execute(text(_CH.format(id="c3", name="retired")))
            await conn.execute(text(
                "UPDATE channels SET required_metadata_fields = "
                "'[\"season\",\"absolute_episode\",\"episode_confidence\"]' "
                "WHERE id = 'c3'"
            ))
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)
        async with engine.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT name, required_metadata_fields FROM channels"
            ))).fetchall()
            by_name = {r[0]: r[1] for r in rows}
            for name in ("null-rf", "bad-json", "retired"):
                stored = by_name[name]
                parsed = stored if isinstance(stored, str) else None
                assert parsed is not None
                import json as _json

                assert _json.loads(parsed) == baseline


async def test_light_migrations_weak_audio_reclassify():
    """stub 音乐 audio_work 且有多文件指派 → 清 audio_work_id 并删 stub。"""
    async with _raw_turso_engine() as engine:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO audio_works (id, content_type, external_source) "
                "VALUES ('aw1', 'music', 'stub')"
            ))
            await conn.execute(text(
                "INSERT INTO file_resources (id, channel_id, guid, title_raw, "
                "torrent_url, is_batch, audio_work_id) "
                "VALUES ('fr1', 'c1', 'g1', 'x', 'http://x', 1, 'aw1')"
            ))
            await conn.execute(text(
                "INSERT INTO resource_file_assignments (id, resource_id, file_path, "
                "episode_start, episode_end, source) "
                "VALUES ('rfa1', 'fr1', '/a.mp4', 1, 1, 'auto')"
            ))
            await conn.execute(text(
                "INSERT INTO resource_file_assignments (id, resource_id, file_path, "
                "episode_start, episode_end, source) "
                "VALUES ('rfa2', 'fr1', '/b.mp4', 2, 2, 'auto')"
            ))
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)
        async with engine.begin() as conn:
            row = (await conn.execute(text(
                "SELECT audio_work_id, metadata_attempts FROM file_resources WHERE id = 'fr1'"
            ))).one()
            assert row.audio_work_id is None
            assert row.metadata_attempts == 0
            count = (await conn.execute(text(
                "SELECT COUNT(*) FROM audio_works WHERE id = 'aw1'"
            ))).scalar_one()
            assert count == 0


async def test_light_migrations_single_season_batch_coverage_sync():
    """单季批次资源由文件指派同步 resource 级 season/episode_start/end。"""
    async with _raw_turso_engine() as engine:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO tv_series (id, title_en, content_type) VALUES ('s1', 'S', 'tv')"
            ))
            await conn.execute(text(
                "INSERT INTO file_resources (id, channel_id, guid, title_raw, "
                "torrent_url, is_batch, batch_scope, series_id) "
                "VALUES ('fr1', 'c1', 'g1', 'x', 'http://x', 1, 'season', 's1')"
            ))
            await conn.execute(text(
                "INSERT INTO resource_file_assignments (id, resource_id, file_path, "
                "season, episode_start, episode_end, source) "
                "VALUES ('rfa1', 'fr1', '/a.mp4', 2, 3, 3, 'auto')"
            ))
            await conn.execute(text(
                "INSERT INTO resource_file_assignments (id, resource_id, file_path, "
                "season, episode_start, episode_end, source) "
                "VALUES ('rfa2', 'fr1', '/b.mp4', 2, 4, 4, 'auto')"
            ))
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)
        async with engine.begin() as conn:
            row = (await conn.execute(text(
                "SELECT season, episode_start, episode_end FROM file_resources WHERE id = 'fr1'"
            ))).one()
            assert (row.season, row.episode_start, row.episode_end) == (2, 3, 4)


async def test_light_migrations_work_external_ids_seed():
    """作品主 id 落身份袋（work_external_ids 种子）。"""
    async with _raw_turso_engine() as engine:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO tv_series (id, title_en, content_type, external_source, external_id) "
                "VALUES ('s1', 'S', 'tv', 'wikipedia', '12345')"
            ))
            await conn.execute(text(
                "INSERT INTO movies (id, title_en, content_type, external_source, external_id) "
                "VALUES ('m1', 'M', 'movie', 'tmdb', '999')"
            ))
            await conn.execute(text(
                "INSERT INTO tv_series (id, title_en, content_type, external_source, external_id) "
                "VALUES ('s2', 'S2', 'tv', 'manual', 'x')"
            ))
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)
        async with engine.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT work_type, work_id, source, external_id FROM work_external_ids"
            ))).fetchall()
            assert {tuple(r) for r in rows} == {
                ("series", "s1", "wikipedia", "12345"),
                ("movie", "m1", "tmdb", "999"),
            }
            await _apply_light_migrations(conn)  # 幂等：不重复种子
            count = (await conn.execute(text(
                "SELECT COUNT(*) FROM work_external_ids"
            ))).scalar_one()
            assert count == 2


async def test_light_migrations_download_notifications_rebuild():
    """旧 download_notifications 带 status 等投递列 → Turso 表重建去列。"""
    async with _raw_turso_engine() as engine:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE download_notifications"))
            await conn.execute(text("""
                CREATE TABLE download_notifications (
                  id VARCHAR(36) NOT NULL PRIMARY KEY,
                  agent_id VARCHAR(36),
                  download_task_id VARCHAR(36) NOT NULL,
                  payload JSON NOT NULL,
                  status VARCHAR(16) NOT NULL DEFAULT 'pending',
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  error_message TEXT,
                  created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
            """))
            await conn.execute(text(
                "INSERT INTO download_notifications (id, download_task_id, payload, status) "
                "VALUES (:id, :tid, :p, 'pending')"),
                {"id": "n1", "tid": "t1", "p": '{"x":1}'})
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)
            cols = (await conn.execute(
                text("PRAGMA table_info(download_notifications)")
            )).fetchall()
            names = {row[1] for row in cols}
            assert "status" not in names
            assert "attempt_count" not in names
            row = (await conn.execute(text(
                "SELECT id, payload FROM download_notifications"
            ))).one()
            assert row.id == "n1"


async def test_light_migrations_libraries_root_path_rebuild():
    """旧 libraries.root_path NOT NULL → Turso 表重建放宽。"""
    async with _raw_turso_engine() as engine:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE libraries"))
            await conn.execute(text("""
                CREATE TABLE libraries (
                  id VARCHAR(36) NOT NULL PRIMARY KEY,
                  name VARCHAR(255) NOT NULL,
                  root_path VARCHAR(1024) NOT NULL,
                  kind VARCHAR(16) NOT NULL,
                  plex_section VARCHAR(64),
                  subtitle_lang_map JSON,
                  created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
            """))
            await conn.execute(text(
                "INSERT INTO libraries (id, name, root_path, kind) "
                "VALUES ('l1', 'M', '/x', 'movie')"
            ))
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)
            cols = (await conn.execute(text("PRAGMA table_info(libraries)"))).fetchall()
            notnull = {row[1]: row[3] for row in cols}
            assert not notnull.get("root_path")
            assert "volume_id" in {row[1] for row in cols}
            row = (await conn.execute(text(
                "SELECT id, name FROM libraries"
            ))).one()
            assert row.id == "l1"


async def test_light_migrations_batch_scope_movies_rewrite():
    """纯电影合集 franchise → batch_scope='movies' 改写。"""
    async with _raw_turso_engine() as engine:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO work_collections (id, title_cn) VALUES ('wc1', 'C1')"
            ))
            await conn.execute(text(
                "INSERT INTO work_collections (id, title_cn) VALUES ('wc2', 'C2')"
            ))
            await conn.execute(text(
                "INSERT INTO movies (id, title_en, content_type, collection_id) "
                "VALUES ('m1', 'M', 'movie', 'wc1')"
            ))
            await conn.execute(text(
                "INSERT INTO file_resources (id, channel_id, guid, title_raw, "
                "torrent_url, is_batch, batch_scope, collection_id) "
                "VALUES ('fr1', 'c1', 'g1', 'x', 'http://x', 1, 'franchise', 'wc1')"
            ))
            await conn.execute(text(
                "INSERT INTO file_resources (id, channel_id, guid, title_raw, "
                "torrent_url, is_batch, batch_scope, collection_id) "
                "VALUES ('fr2', 'c1', 'g2', 'x', 'http://x', 1, 'franchise', 'wc1')"
            ))
            await conn.execute(text(
                "INSERT INTO tv_series (id, title_en, content_type, collection_id) "
                "VALUES ('s1', 'S', 'tv', 'wc2')"
            ))
            await conn.execute(text(
                "INSERT INTO file_resources (id, channel_id, guid, title_raw, "
                "torrent_url, is_batch, batch_scope, collection_id) "
                "VALUES ('fr3', 'c1', 'g3', 'x', 'http://x', 1, 'franchise', 'wc2')"
            ))
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)
        async with engine.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT id, batch_scope FROM file_resources WHERE id IN ('fr1','fr2','fr3')"
            ))).fetchall()
            by_id = {r[0]: r[1] for r in rows}
            assert by_id["fr1"] == "movies"
            assert by_id["fr2"] == "movies"
            assert by_id["fr3"] == "franchise"  # 合集含 tv_series 时不改写


# ---------------------------------------------------------------------------
# PostgreSQL 分支（真实 PG 可用时执行，否则跳过）
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_scratch():
    """Fresh scratch database URL on a reachable PostgreSQL (skip otherwise)."""
    host_url = os.environ.get(
        "RSSRIPPLE_TEST_POSTGRES_URL",
        "postgresql+asyncpg://rssripple:rssripple@127.0.0.1:5432/rssripple",
    )
    try:
        admin = create_async_engine(
            host_url, isolation_level="AUTOCOMMIT", connect_args={"timeout": 3}
        )
        async with admin.connect() as conn:
            await conn.execute(text("SELECT 1"))
            await conn.execute(text("DROP DATABASE IF EXISTS rssripple_unit_db"))
            await conn.execute(text("CREATE DATABASE rssripple_unit_db"))
        await admin.dispose()
    except Exception as e:  # noqa: BLE001 - environment may have no postgres
        pytest.skip(f"PostgreSQL unavailable: {e!r}")
    scratch = host_url.rsplit("/", 1)[0] + "/rssripple_unit_db"
    yield scratch
    try:
        admin = create_async_engine(
            host_url, isolation_level="AUTOCOMMIT", connect_args={"timeout": 3}
        )
        async with admin.connect() as conn:
            await conn.execute(text("DROP DATABASE IF EXISTS rssripple_unit_db"))
        await admin.dispose()
    except Exception:  # noqa: BLE001
        pass


@pytest_asyncio.fixture
async def pg_env(pg_scratch):
    """Swap the module-global engine/factory/settings onto the scratch DB."""
    import app.database as db_mod
    from app.config import settings

    old_url = settings.database_url
    old_engine = db_mod.engine
    old_factory = db_mod.async_session_factory
    settings.database_url = pg_scratch
    eng = create_async_engine(pg_scratch)
    db_mod.engine = eng
    db_mod.async_session_factory = async_sessionmaker(
        eng, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield eng
    finally:
        db_mod.engine = old_engine
        db_mod.async_session_factory = old_factory
        settings.database_url = old_url
        await eng.dispose()


async def test_create_tables_postgres_path(pg_env):
    """create_tables 的 PostgreSQL 分支（lock_timeout + advisory lock +
    create_all + 轻迁移 + pg_trgm + search_text 回填）。"""
    from app.database import create_tables

    await create_tables()
    async with pg_env.connect() as conn:
        rows = await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ))
        names = {r[0] for r in rows}
        assert "tv_series" in names
        assert "work_collections" in names
        assert "download_notifications" in names
        cols = await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'tv_series'"
        ))
        assert "season_number" in {r[0] for r in cols}


async def test_light_migrations_postgres_legacy_shapes(pg_env):
    """PostgreSQL 分支：枚举扩展、download_tasks FK 放宽、旧投递列
    NOT NULL 放宽、required_metadata_fields 各收敛分支。"""
    from app.database import create_tables

    await create_tables()
    async with pg_env.begin() as conn:
        await conn.execute(text(
            "CREATE TYPE downloader_type AS ENUM ('transmission')"
        ))
        await conn.execute(text(
            "ALTER TABLE download_notifications "
            "ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'pending'"
        ))
        await conn.execute(text(
            "ALTER TABLE download_notifications "
            "ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
        ))
        await conn.execute(text(
            "INSERT INTO channels (id, name, type, url, fetch_interval, status, "
            "field_mapping, metadata_agent_enabled, required_metadata_fields) "
            "VALUES ('c1', 'season-only', 'rss_feed', 'https://c1', 1800, 'active', "
            "'{}', TRUE, '[\"season\"]')"
        ))
        await conn.execute(text(
            "INSERT INTO channels (id, name, type, url, fetch_interval, status, "
            "field_mapping, metadata_agent_enabled, required_metadata_fields) "
            "VALUES ('c2', 'titles', 'rss_feed', 'https://c2', 1800, 'active', "
            "'{}', TRUE, '[\"title_cn\",\"title_en\"]')"
        ))
        await conn.execute(text(
            "UPDATE app_settings SET value = 'pending' WHERE key IN ("
            "'required_fields_title_cn_compat_v1', "
            "'required_fields_title_cn_unlock_v1', "
            "'required_fields_title_en_unlock_v1', "
            "'required_fields_per_season_v1')"
        ))
        await _apply_light_migrations(conn)
        await _apply_light_migrations(conn)  # 幂等

    async with pg_env.connect() as conn:
        enum = (await conn.execute(text(
            "SELECT enumlabel FROM pg_enum JOIN pg_type "
            "ON pg_enum.enumtypid = pg_type.oid WHERE typname = 'downloader_type'"
        ))).fetchall()
        assert "mock" in {r[0] for r in enum}

        rows = (await conn.execute(text(
            "SELECT name, required_metadata_fields FROM channels"
        ))).fetchall()
        import json as _json

        from app.services.required_fields import normalize_required_fields

        baseline = normalize_required_fields([])
        for _, rf in rows:
            parsed = _json.loads(rf) if isinstance(rf, str) else list(rf or [])
            assert parsed == baseline


async def test_light_migrations_postgres_legacy_delivery_columns_nullable(pg_env):
    """download_notifications 的 status/attempt_count NOT NULL 在 PG 上被放宽。"""
    from app.database import create_tables

    await create_tables()
    async with pg_env.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE download_notifications "
            "ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'pending'"
        ))
        await conn.execute(text(
            "ALTER TABLE download_notifications "
            "ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
        ))
        await _apply_light_migrations(conn)
    async with pg_env.connect() as conn:
        cols = await conn.execute(text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'download_notifications' AND column_name = 'status'"
        ))
        row = cols.first()
        assert row is not None and row.is_nullable == "YES"


def test_install_db_retry_middleware_turso_reraises_non_retryable():
    """非可重试错误不重试，直接抛给上层。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    state = {"calls": 0}

    @app.get("/boom")
    async def boom():
        state["calls"] += 1
        raise _db_error("connection refused")

    install_db_retry_middleware(app)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert state["calls"] == 1


async def test_create_tables_postgres_retries_on_lock_timeout(monkeypatch):
    """PostgreSQL 启动 DDL 遇 lock_timeout → 有限重试后成功。"""
    import app.database as db_mod
    from app.database import _create_tables_postgres

    state = {"calls": 0}

    class _FakeConn:
        async def execute(self, stmt, params=None):
            if state["calls"] == 0:
                state["calls"] += 1
                exc = _db_error("lock not available")
                exc.orig = SimpleNamespace(sqlstate="55P03")
                raise exc
            return SimpleNamespace()

        def run_sync(self, fn):
            async def _done(*a, **kw):
                return None

            return _done()

    class _FakeEngine:
        @contextlib.asynccontextmanager
        async def begin(self):
            yield _FakeConn()

    monkeypatch.setattr(db_mod, "engine", _FakeEngine())
    monkeypatch.setattr(db_mod, "_apply_light_migrations", AsyncMock())
    monkeypatch.setattr(db_mod, "_ensure_pg_trgm_indexes", AsyncMock())
    await _create_tables_postgres()
    assert state["calls"] == 1


async def test_create_tables_postgres_reraises_non_lock_error(monkeypatch):
    """PostgreSQL 启动 DDL 遇非 lock_timeout 错误 → 立即抛出。"""
    import app.database as db_mod
    from app.database import _create_tables_postgres

    class _FakeConn:
        async def execute(self, stmt, params=None):
            raise _db_error("boom")

    class _FakeEngine:
        @contextlib.asynccontextmanager
        async def begin(self):
            yield _FakeConn()

    monkeypatch.setattr(db_mod, "engine", _FakeEngine())
    monkeypatch.setattr(db_mod, "_apply_light_migrations", AsyncMock())
    monkeypatch.setattr(db_mod, "_ensure_pg_trgm_indexes", AsyncMock())
    with pytest.raises(DatabaseError):
        await _create_tables_postgres()


async def test_light_migrations_required_fields_defensive_branches():
    """单测哨兵恢复后重跑：NULL/非法 JSON 在 unlock/退役/基线各分支。"""
    async with _raw_turso_engine() as engine:
        async with engine.begin() as conn:
            await _apply_light_migrations(conn)  # 先让所有哨兵置 done

        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO channels (id, name, type, url, fetch_interval, status, "
                "field_mapping, metadata_agent_enabled, metadata_source, "
                "required_metadata_fields) VALUES ('c1', 'bad', 'rss_feed', "
                "'https://c1', 1800, 'active', '{}', 0, 'wikipedia', 'not-json')"
            ))
            await conn.execute(text(
                "INSERT INTO channels (id, name, type, url, fetch_interval, status, "
                "field_mapping, metadata_agent_enabled, metadata_source, "
                "required_metadata_fields) VALUES ('c2', 'null', 'rss_feed', "
                "'https://c2', 1800, 'active', '{}', 0, 'wikipedia', NULL)"
            ))
            # compat 已 done，避免它先把非法 JSON 归一化掉；
            # unlock/退役/基线各块重跑覆盖其 ValueError/else 分支。
            await conn.execute(text(
                "UPDATE app_settings SET value = 'pending' WHERE key IN ("
                "'required_fields_title_cn_unlock_v1', "
                "'required_fields_title_en_unlock_v1', "
                "'required_fields_per_season_v1')"
            ))
            await _apply_light_migrations(conn)

        async with engine.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT name, required_metadata_fields FROM channels"
            ))).fetchall()
            import json as _json

            from app.services.required_fields import normalize_required_fields

            baseline = normalize_required_fields([])
            for _, rf in rows:
                assert _json.loads(rf) == baseline


# ---------------------------------------------------------------------------
# PostgreSQL-only migration branches via a fake async connection
# ---------------------------------------------------------------------------


class _FakeSavepoint:
    async def start(self):
        return self

    async def rollback(self):
        return None

    async def commit(self):
        return None


class _FakeRow:
    """Result row supporting integer indexing and iteration (no attributes)."""

    def __init__(self, *values):
        self._values = tuple(values)

    def __getitem__(self, index):
        return self._values[index]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class _FakeChannelRow:
    """Channels select row: the migration reads ``row.id`` /
    ``row.required_metadata_fields`` by attribute."""

    def __init__(self, id_, required_metadata_fields):
        self.id = id_
        self.required_metadata_fields = required_metadata_fields

    def __getitem__(self, index):
        return (self.id, self.required_metadata_fields)[index]

    def __iter__(self):
        return iter((self.id, self.required_metadata_fields))


class _FakeResult:
    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.rowcount = len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0][0] if self._rows else None

    def scalar_one(self):
        return self._rows[0][0] if self._rows else 0

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakePGConn:
    """Minimal async connection emulating enough PG catalog behaviour to walk
    every PostgreSQL-only branch of ``_apply_light_migrations``."""

    def __init__(self, channel_rows):
        self._channel_rows = channel_rows
        self.executed: list[str] = []

    def begin_nested(self):
        return _FakeSavepoint()

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append(sql)
        if "information_schema.columns" in sql:
            if params and params.get("t") is not None:
                return _FakeResult([])
            if "'agents'" in sql:
                return _FakeResult([])
            if "'download_notifications'" in sql:
                return _FakeResult([_FakeRow("status"), _FakeRow("attempt_count")])
            return _FakeResult([])
        if "SELECT value FROM app_settings" in sql:
            return _FakeResult([("pending",)])
        if "SELECT id, required_metadata_fields FROM channels" in sql:
            return _FakeResult(self._channel_rows)
        if "SELECT 1 FROM pg_type" in sql:
            return _FakeResult([(1,)])
        if "COUNT(*)" in sql:
            return _FakeResult([(0,)])
        return _FakeResult([])


async def test_light_migrations_postgres_branches_fake_conn(monkeypatch):
    """Walk the PostgreSQL-only migration branches without a real server."""
    import app.database as db_mod
    from app.config import settings

    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://u:p@h/db"
    )
    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "tok")

    rows = [
        _FakeChannelRow("c1", '["season"]'),
        _FakeChannelRow(
            "c2",
            '["title_cn","title_en","season","absolute_episode","episode_confidence"]',
        ),
    ]
    conn = _FakePGConn(rows)
    await db_mod._apply_light_migrations(conn)
    # Spot-check the queries actually ran.
    assert any("pg_advisory" not in q for q in conn.executed)
    assert any("ALTER TABLE libraries ALTER COLUMN root_path DROP NOT NULL" in q
               for q in conn.executed)
    assert any("ADD VALUE IF NOT EXISTS 'mock'" in q for q in conn.executed)
    assert any("download_tasks_agent_id_fkey" in q for q in conn.executed)


async def test_ensure_pg_trgm_indexes_fake_conn():
    import app.database as db_mod

    conn = _FakePGConn([])
    await db_mod._ensure_pg_trgm_indexes(conn)
    assert any("CREATE EXTENSION IF NOT EXISTS pg_trgm" in q for q in conn.executed)
    assert sum("USING gin" in q for q in conn.executed) == 3


async def test_create_tables_postgres_branch(monkeypatch):
    import app.database as db_mod
    from app.config import settings
    from app.services import fts as fts_mod

    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://u:p@h/db"
    )
    called: dict[str, bool] = {}

    async def _fake_create():
        called["create"] = True

    monkeypatch.setattr(db_mod, "_create_tables_postgres", _fake_create)

    async def _fake_backfill(session):
        called["backfill"] = True

    monkeypatch.setattr(fts_mod, "backfill_search_text", _fake_backfill)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            called["commit"] = True

    monkeypatch.setattr(db_mod, "async_session_factory", lambda: _Session())
    await db_mod.create_tables()
    assert called == {"create": True, "backfill": True, "commit": True}


async def test_retry_on_lock_unreachable(monkeypatch):
    import app.database as db_mod

    monkeypatch.setattr(db_mod, "_MAX_DB_RETRIES", 0)

    async def _op():
        return 1

    with pytest.raises(AssertionError):
        await db_mod.retry_on_lock(_op)


def test_install_db_retry_middleware_unreachable(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.database as db_mod

    monkeypatch.setattr(db_mod, "_MAX_DB_RETRIES", 0)
    app = FastAPI()

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    db_mod.install_db_retry_middleware(app)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/ok").status_code == 500
