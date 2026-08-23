"""Unit tests for ``_apply_light_migrations`` channel metadata-source
convergence."""

from sqlalchemy import select, text

from app.database import _apply_light_migrations
from app.models.channel import Channel


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
