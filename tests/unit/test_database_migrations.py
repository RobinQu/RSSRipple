"""Unit tests for ``_apply_light_migrations`` channel metadata-source
convergence (Phase P1 two-source architecture)."""

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
