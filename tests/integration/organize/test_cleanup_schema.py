"""Real Turso fresh schema and repeatable upgrade installation."""

from sqlalchemy import text

from app.database import _apply_light_migrations


async def test_parent_guards_are_installed_by_fresh_schema_and_upgrade(db_engine):
    catalog = text("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_resource_parent_%' ORDER BY name")
    async with db_engine.connect() as conn:
        fresh = (await conn.execute(catalog)).all()
        assert len(fresh) == 6
        # All identifiers originate from the catalog of this fresh test DB.
        for name, _ in fresh:
            assert name.replace("_", "").isalnum()
            await conn.exec_driver_sql(f'DROP TRIGGER "{name}"')
        await conn.commit()
        assert not (await conn.execute(catalog)).all()
        await _apply_light_migrations(conn)
        await conn.commit()
        assert (await conn.execute(catalog)).all() == fresh
        await _apply_light_migrations(conn)
        await conn.commit()
        assert (await conn.execute(catalog)).all() == fresh


async def test_parent_rebuild_failure_restores_guards_and_rows(db_engine, db_session):
    import re
    import uuid

    import pytest
    from sqlalchemy import event, select
    from sqlalchemy.schema import CreateTable

    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.resource_work_link import ResourceWorkLink
    from app.services.schema_foreign_keys import repair_turso_foreign_keys
    from tests.unit.test_resource_cleanup import _make_resource

    ddl = str(CreateTable(FileResource.__table__).compile(dialect=db_engine.dialect))
    ddl = "\n".join(line for line in ddl.splitlines() if "FOREIGN KEY(collection_id)" not in line)
    ddl = re.sub(r",\s*\)", "\n)", ddl)
    async with db_engine.begin() as conn:
        await conn.execute(text("DROP TABLE file_resources"))
        await conn.execute(text(ddl))
    channel = Channel(id=str(uuid.uuid4()), name="Synthetic migration rollback", type="rss_feed",
                      url="https://example.invalid/rss", field_mapping={})
    movie = Movie(id=str(uuid.uuid4()), title_cn="Synthetic movie")
    db_session.add_all([channel, movie])
    await db_session.flush()
    resource = _make_resource(channel.id)
    db_session.add(resource)
    await db_session.flush()
    link = ResourceWorkLink(id=str(uuid.uuid4()), resource_id=resource.id, movie_id=movie.id)
    db_session.add(link)
    await db_session.commit()
    resource_id, link_id = resource.id, link.id
    catalog = text("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_resource_parent_%' ORDER BY name")
    async with db_engine.connect() as conn:
        expected = (await conn.execute(catalog)).all()
        assert len(expected) == 6

    def fail_rename(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("ALTER TABLE __fk_repair_file_resources RENAME"):
            raise RuntimeError("Injected after parent drop, before rename")

    event.listen(db_engine.sync_engine, "before_cursor_execute", fail_rename)
    try:
        with pytest.raises(RuntimeError, match="Injected after parent drop"):
            await repair_turso_foreign_keys(db_engine)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", fail_rename)
    async with db_engine.connect() as conn:
        assert (await conn.execute(catalog)).all() == expected
        assert await conn.scalar(select(FileResource.id).where(FileResource.id == resource_id)) == resource_id
        assert await conn.scalar(select(ResourceWorkLink.id).where(ResourceWorkLink.id == link_id)) == link_id
    await repair_turso_foreign_keys(db_engine)
    async with db_engine.connect() as conn:
        assert (await conn.execute(catalog)).all() == expected
        parents = (await conn.execute(text("PRAGMA foreign_key_list('file_resources')"))).all()
        assert any(row[2:5] == ("work_collections", "collection_id", "id") for row in parents)
        assert await conn.scalar(text(
            "SELECT count(*) FROM resource_work_links child LEFT JOIN file_resources parent "
            "ON parent.id = child.resource_id WHERE parent.id IS NULL"
        )) == 0
        assert await conn.scalar(select(ResourceWorkLink.id).where(ResourceWorkLink.id == link_id)) == link_id
