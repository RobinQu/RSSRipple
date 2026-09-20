"""Actual startup must preserve fresh-schema FK enforcement on upgrades."""

import re
import uuid

import pytest
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from app.database import create_tables
from app.models.series import TVSeries


@pytest.mark.parametrize("legacy", [False, True], ids=["fresh", "upgraded"])
async def test_series_collection_fk_after_startup(db_engine, db_session, legacy):
    if legacy:
        # Recreate only the empty series table in its historical shape:
        # collection_id and its FK did not exist. Production startup must
        # add the column and enforce the same parent relationship as new DBs.
        ddl = str(CreateTable(TVSeries.__table__).compile(dialect=db_engine.dialect))
        ddl = "\n".join(
            line
            for line in ddl.splitlines()
            if not re.search(r"^\s*collection_id\s|FOREIGN KEY\(collection_id\)", line)
        )
        ddl = re.sub(r",\s*\)", "\n)", ddl)
        async with db_engine.begin() as conn:
            await conn.execute(text("DROP TABLE tv_series"))
            await conn.execute(text(ddl))
    await create_tables()
    await create_tables()
    async with db_engine.connect() as conn:
        parents = (await conn.execute(text("PRAGMA foreign_key_list('tv_series')"))).all()
        print("series FKs after startup:", parents)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("INSERT INTO tv_series (id, season_number, collection_id) VALUES (:id, 1, :parent)"),
                {"id": str(uuid.uuid4()), "parent": str(uuid.uuid4())},
            )


@pytest.mark.parametrize("legacy", [False, True, "unconstrained"], ids=["fresh", "missing-column", "existing-column"])
@pytest.mark.parametrize(
    "table,column,target,ondelete",
    [
        ("file_resources", "audio_work_id", "audio_works", "SET NULL"),
        ("tv_series", "collection_id", "work_collections", "NO ACTION"),
        ("movies", "collection_id", "work_collections", "NO ACTION"),
        ("downloader_instances", "volume_id", "storage_volumes", "SET NULL"),
        ("libraries", "media_server_id", "media_server_instances", "SET NULL"),
        ("libraries", "volume_id", "storage_volumes", "SET NULL"),
        ("file_resources", "collection_id", "work_collections", "NO ACTION"),
    ],
)
async def test_light_migration_fk_schema_parity(db_engine, legacy, table, column, target, ondelete):
    from app.database import Base

    if legacy:
        ddl = str(CreateTable(Base.metadata.tables[table]).compile(dialect=db_engine.dialect))
        # A historical table cannot have either the new column's FK or a
        # compound constraint referencing the not-yet-existing column.
        patterns = rf"FOREIGN KEY\({column}\)"
        if legacy is True:
            patterns += rf"|^\s*{column}\s|CONSTRAINT.*\b{column}\b"
        ddl = "\n".join(line for line in ddl.splitlines() if not re.search(patterns, line))
        ddl = re.sub(r",\s*\)", "\n)", ddl)
        async with db_engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE {table}"))
            await conn.execute(text(ddl))
    await create_tables()
    await create_tables()
    async with db_engine.connect() as conn:
        rows = (await conn.execute(text(f"PRAGMA foreign_key_list('{table}')"))).all()
    actual = {(row[3], row[2], row[4], row[6]) for row in rows}
    assert (column, target, "id", ondelete) in actual, actual
    await assert_fk_actions(db_engine, table, column, target, ondelete)


@pytest.mark.parametrize("failure", [None, "orphan", "after-swap"])
async def test_populated_series_repair_preserves_children_and_legacy_schema(db_engine, db_session, failure):
    from app.models.channel import Channel
    from app.models.episode import Episode
    from app.models.file_resource import FileResource
    from app.models.work_collection import WorkCollection
    from app.models.work_external_id import WorkExternalId

    ddl = str(CreateTable(TVSeries.__table__).compile(dialect=db_engine.dialect))
    ddl = "\n".join(line for line in ddl.splitlines() if "FOREIGN KEY(collection_id)" not in line)
    ddl = re.sub(r",\s*\)", "\n)", ddl)
    async with db_engine.begin() as conn:
        await conn.execute(text("DROP TABLE tv_series"))
        await conn.execute(text(ddl))
        await conn.execute(text("ALTER TABLE tv_series ADD COLUMN historical_note TEXT"))
        await conn.execute(text("CREATE UNIQUE INDEX ix_historical_note ON tv_series(historical_note)"))
    parent = WorkCollection(title_cn="Synthetic preserved parent")
    channel = Channel(name="Synthetic preserved channel", url="https://example.invalid/rss", field_mapping={})
    db_session.add_all([parent, channel])
    await db_session.flush()
    work = TVSeries(
        title_cn="Synthetic manually edited work",
        collection_id=parent.id,
        season_number=3,
        manually_edited_fields=["title_cn"],
    )
    db_session.add(work)
    await db_session.flush()
    episode = Episode(series_id=work.id, season=3, episode=1)
    resource = FileResource(
        channel_id=channel.id,
        guid="synthetic-fk-preserve",
        title_raw="Synthetic S03E01",
        torrent_url="https://example.invalid/test.torrent",
        series_id=work.id,
        collection_id=parent.id,
        season=3,
        episode=1,
    )
    bag = WorkExternalId(work_type="series", work_id=work.id, source="tmdb", external_id="tmdb:900001#s3")
    db_session.add_all([episode, resource, bag])
    await db_session.execute(text("UPDATE tv_series SET historical_note='preserve me' WHERE id=:id"), {"id": work.id})
    await db_session.commit()
    ids = (work.id, parent.id, episode.id, resource.id, bag.id)
    if failure:
        before = {}
        async with db_engine.begin() as conn:
            if failure == "orphan":
                await conn.execute(
                    text("UPDATE tv_series SET collection_id='missing-parent' WHERE id=:id"), {"id": ids[0]}
                )
            for table in ("tv_series", "episodes", "file_resources", "work_external_ids"):
                before[table] = (await conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))).all()

        def fail_after_swap(conn, cursor, statement, parameters, context, executemany):
            if "__fk_repair_tv_series" in statement and "RENAME TO" in statement:
                raise RuntimeError("injected after swap")

        if failure == "after-swap":
            event.listen(db_engine.sync_engine, "after_cursor_execute", fail_after_swap)
        try:
            with pytest.raises(RuntimeError, match="orphan row IDs|injected after swap"):
                await create_tables()
        finally:
            if failure == "after-swap":
                event.remove(db_engine.sync_engine, "after_cursor_execute", fail_after_swap)
        async with db_engine.begin() as conn:
            for table, rows in before.items():
                assert (await conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))).all() == rows
            assert await conn.scalar(text("PRAGMA foreign_keys")) == 1
            assert not (await conn.execute(text("PRAGMA foreign_key_list(tv_series)"))).all()
            assert await conn.scalar(text("SELECT count(*) FROM sqlite_master WHERE name LIKE '__fk_repair_%'")) == 0
            if failure == "orphan":
                await conn.execute(
                    text("UPDATE tv_series SET collection_id=:parent WHERE id=:id"), {"parent": ids[1], "id": ids[0]}
                )
    await create_tables()
    await create_tables()
    db_session.expire_all()
    work = await db_session.get(TVSeries, ids[0])
    assert (work.collection_id, work.season_number, work.title_cn, work.manually_edited_fields) == (
        ids[1],
        3,
        "Synthetic manually edited work",
        ["title_cn"],
    )
    assert (await db_session.get(Episode, ids[2])).series_id == ids[0]
    resource = await db_session.get(FileResource, ids[3])
    assert (resource.series_id, resource.collection_id) == (ids[0], ids[1])
    assert (await db_session.get(WorkExternalId, ids[4])).work_id == ids[0]
    assert (
        await db_session.scalar(text("SELECT historical_note FROM tv_series WHERE id=:id"), {"id": ids[0]})
        == "preserve me"
    )
    assert await db_session.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='ix_historical_note'")) == 1


async def assert_fk_actions(engine, table, column, target, ondelete):
    """Exercise SQL directly: ORM relationship cascades cannot mask FK behavior."""
    from app.database import Base

    tables = Base.metadata.tables
    parent_id, child_id = str(uuid.uuid4()), str(uuid.uuid4())
    parent_values = {
        "work_collections": {"title_cn": "Synthetic FK parent"},
        "audio_works": {"title_cn": "Synthetic FK audio"},
        "storage_volumes": {"name": parent_id, "mount_path": "/synthetic"},
        "media_server_instances": {"name": parent_id, "type": "plex", "url": "https://example.invalid"},
    }[target]
    child_values = {
        "tv_series": {"title_cn": "Synthetic FK series", "season_number": 1},
        "movies": {"title_cn": "Synthetic FK movie"},
        "libraries": {"name": child_id},
        "downloader_instances": {"name": child_id, "url": "https://example.invalid", "download_dir": "/synthetic"},
        "file_resources": {
            "guid": child_id,
            "title_raw": "Synthetic FK resource",
            "torrent_url": "https://example.invalid/torrent",
        },
    }[table]
    async with engine.begin() as conn:
        if table == "file_resources":
            channel_id = str(uuid.uuid4())
            await conn.execute(
                insert(tables["channels"]).values(
                    id=channel_id,
                    name=channel_id,
                    url="https://example.invalid/rss",
                    field_mapping={},
                )
            )
            child_values["channel_id"] = channel_id
        await conn.execute(insert(tables[target]).values(id=parent_id, **parent_values))
        await conn.execute(insert(tables[table]).values(id=child_id, **child_values, **{column: parent_id}))
        with pytest.raises(IntegrityError, match="(?i)foreign key"):
            async with conn.begin_nested():
                await conn.execute(
                    update(tables[table]).where(tables[table].c.id == child_id).values(**{column: str(uuid.uuid4())})
                )
        invalid_values = dict(child_values)
        if table == "file_resources":
            invalid_values["guid"] = str(uuid.uuid4())
        with pytest.raises(IntegrityError, match="(?i)foreign key"):
            async with conn.begin_nested():
                await conn.execute(
                    insert(tables[table]).values(
                        id=str(uuid.uuid4()),
                        **invalid_values,
                        **{column: str(uuid.uuid4())},
                    )
                )
        removal = delete(tables[target]).where(tables[target].c.id == parent_id)
        if ondelete == "SET NULL":
            await conn.execute(removal)
        else:
            with pytest.raises(IntegrityError, match="(?i)foreign key"):
                async with conn.begin_nested():
                    await conn.execute(removal)
        row = (await conn.execute(select(tables[table].c[column]).where(tables[table].c.id == child_id))).one()
        assert row[0] == (None if ondelete == "SET NULL" else parent_id)


@pytest.mark.parametrize("keep_correct_constraint", [False, True])
async def test_existing_conflicting_fk_semantics_rejected(db_engine, keep_correct_constraint):
    ddl = str(CreateTable(TVSeries.__table__).compile(dialect=db_engine.dialect))
    if not keep_correct_constraint:
        ddl = "\n".join(line for line in ddl.splitlines() if "FOREIGN KEY(collection_id)" not in line)
        ddl = re.sub(r",\s*\)", "\n)", ddl)
    end = ddl.rindex(")")
    ddl = ddl[:end] + ", FOREIGN KEY(collection_id) REFERENCES work_collections(id) ON DELETE CASCADE" + ddl[end:]
    async with db_engine.begin() as conn:
        await conn.execute(text("DROP TABLE tv_series"))
        await conn.execute(text(ddl))
        before = (await conn.execute(text("PRAGMA foreign_key_list(tv_series)"))).all()
    with pytest.raises(RuntimeError, match="Unexpected foreign key semantics for tv_series.collection_id"):
        await create_tables()
    async with db_engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA foreign_key_list(tv_series)"))).all() == before
