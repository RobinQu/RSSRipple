"""Schema-hardening regression tests against the fresh-database shape.

Covers the model-level contracts fixed in this round:
- ``Episode.season`` carries a server default (1) in addition to the ORM default.
- ``SubtitleGroupMapping.normalized_key`` is covered by its UniqueConstraint
  only — no redundant single-column index.
- ``external_id`` is VARCHAR(128) on every work table (unified with the
  identity bag) and ``WorkExternalId`` has ``updated_at``.
- ``AgentWork`` target FKs are ON DELETE CASCADE (SET NULL would violate the
  XOR check constraint and block work deletion).
- Unique constraints on ``channels.url``, ``downloader_instances.(name, url)``,
  ``media_server_instances.(name, url)`` and
  ``media_server_bindings.(server_id, server_path_prefix)``.
"""

import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Episode.season server default
# ---------------------------------------------------------------------------


async def test_episode_season_server_default(db_engine, db_session, sample_series):
    from app.models.episode import Episode

    await db_session.commit()  # make the fixture series visible to the raw conn
    async with db_engine.begin() as conn:
        info = (await conn.execute(text("PRAGMA table_info(episodes)"))).fetchall()
        row = next(r for r in info if r[1] == "season")
        assert row[3] == 1  # NOT NULL
        assert row[4] == "'1'"  # server default (rendered as a quoted literal)
        # A raw insert omitting season gets the server default.
        episode_id = _uuid()
        await conn.execute(
            text("INSERT INTO episodes (id, series_id, episode) VALUES (:id, :sid, 1)"),
            {"id": episode_id, "sid": sample_series.id},
        )
    db_session.expire_all()
    ep = await db_session.get(Episode, episode_id)
    assert ep.season == 1


# ---------------------------------------------------------------------------
# SubtitleGroupMapping: unique constraint without redundant index
# ---------------------------------------------------------------------------


async def test_subtitle_group_mapping_unique_without_redundant_index(db_engine, db_session):
    from app.models.subtitle_group_mapping import SubtitleGroupMapping

    async with db_engine.begin() as conn:
        indexes = (
            await conn.execute(text("PRAGMA index_list(subtitle_group_mappings)"))
        ).fetchall()
        names = {row[1] for row in indexes}
        assert "ix_subtitle_group_mappings_normalized_key" not in names

    db_session.add(SubtitleGroupMapping(raw_value="A", normalized_key="a", groups=["A"]))
    await db_session.commit()
    db_session.add(SubtitleGroupMapping(raw_value="B", normalized_key="a", groups=["B"]))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# external_id length + WorkExternalId.updated_at
# ---------------------------------------------------------------------------


def test_external_id_length_unified_at_128():
    for table_name in (
        "tv_series", "movies", "work_collections", "audio_works", "work_external_ids"
    ):
        column = Base.metadata.tables[table_name].c.external_id
        assert column.type.length == 128, table_name


async def test_work_external_id_updated_at(db_session):
    from app.models.work_external_id import WorkExternalId

    row = WorkExternalId(
        work_type="series", work_id=_uuid(), source="tmdb", external_id="tmdb:1"
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.created_at is not None
    assert row.updated_at is not None

    previous = row.updated_at
    row.external_id = "tmdb:2"
    await db_session.flush()
    await db_session.refresh(row)
    assert row.updated_at >= previous  # onupdate fires (second resolution)
    await db_session.rollback()


# ---------------------------------------------------------------------------
# AgentWork ON DELETE CASCADE
# ---------------------------------------------------------------------------


def test_agent_work_target_fks_are_cascade():
    table = Base.metadata.tables["agent_works"]
    actions = {
        fk.parent.name: fk.ondelete
        for column in (table.c.series_id, table.c.movie_id)
        for fk in column.foreign_keys
    }
    assert actions == {"series_id": "CASCADE", "movie_id": "CASCADE"}


async def test_agent_work_cascade_on_series_delete(
    db_session, sample_channel, sample_downloader, sample_series
):
    """Core-level delete of a subscribed series cascades its AgentWork rows."""
    from sqlalchemy import func

    from app.models.agent import Agent
    from app.models.agent_work import AgentWork
    from app.models.series import TVSeries

    agent = Agent(
        name="A", channel_id=sample_channel.id,
        downloader_id=sample_downloader.id, conflict_resolution="auto",
    )
    db_session.add(agent)
    await db_session.flush()
    work = AgentWork(agent_id=agent.id, content_type="tv", series_id=sample_series.id)
    db_session.add(work)
    await db_session.commit()
    work_id = work.id

    # A Core delete emits SQL directly (no ORM relationship cascade), so the
    # DB-level FK action is what removes the subscription row.
    await db_session.execute(delete(TVSeries).where(TVSeries.id == sample_series.id))
    await db_session.commit()
    count = await db_session.scalar(
        select(func.count()).select_from(AgentWork).where(AgentWork.id == work_id)
    )
    assert count == 0


async def test_agent_work_cascade_on_movie_delete(
    db_session, sample_channel, sample_downloader, sample_movie
):
    from sqlalchemy import func

    from app.models.agent import Agent
    from app.models.agent_work import AgentWork
    from app.models.movie import Movie

    agent = Agent(
        name="A", channel_id=sample_channel.id,
        downloader_id=sample_downloader.id, conflict_resolution="auto",
    )
    db_session.add(agent)
    await db_session.flush()
    work = AgentWork(agent_id=agent.id, content_type="movie", movie_id=sample_movie.id)
    db_session.add(work)
    await db_session.commit()
    work_id = work.id

    await db_session.execute(delete(Movie).where(Movie.id == sample_movie.id))
    await db_session.commit()
    count = await db_session.scalar(
        select(func.count()).select_from(AgentWork).where(AgentWork.id == work_id)
    )
    assert count == 0


# ---------------------------------------------------------------------------
# Unique constraints (fresh schema)
# ---------------------------------------------------------------------------


async def test_channel_url_unique(db_session, sample_channel):
    from app.models.channel import Channel

    db_session.add(
        Channel(name="dup", url=sample_channel.url, field_mapping={})
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_downloader_name_and_url_unique(db_session, sample_downloader):
    from app.models.downloader import DownloaderInstance

    dl_name, dl_url = sample_downloader.name, sample_downloader.url
    await db_session.commit()  # fixture only flushes

    db_session.add(DownloaderInstance(
        name=dl_name, type="mock", url="http://other:9091", download_dir="/dl2",
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    db_session.add(DownloaderInstance(
        name="other", type="mock", url=dl_url, download_dir="/dl2",
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_media_server_name_and_url_unique(db_session):
    from app.models.media_server import MediaServerInstance

    db_session.add(MediaServerInstance(name="plex", type="plex", url="http://plex:32400"))
    await db_session.commit()

    db_session.add(MediaServerInstance(name="plex", type="emby", url="http://emby:8096"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    db_session.add(MediaServerInstance(name="emby", type="emby", url="http://plex:32400"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_media_server_binding_server_prefix_unique(db_session):
    from app.models.media_server import MediaServerBinding, MediaServerInstance
    from app.models.storage_volume import StorageVolume

    server = MediaServerInstance(name="plex", type="plex", url="http://plex:32400")
    volume = StorageVolume(name="vol", mount_path="/mnt/vol")
    db_session.add_all([server, volume])
    await db_session.flush()
    server_id, volume_id = server.id, volume.id
    db_session.add(MediaServerBinding(
        server_id=server_id, server_path_prefix="/data", volume_id=volume_id,
    ))
    await db_session.commit()

    # Same prefix on the same server conflicts; on another server it is fine.
    db_session.add(MediaServerBinding(
        server_id=server_id, server_path_prefix="/data", volume_id=volume_id,
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    other = MediaServerInstance(name="emby", type="emby", url="http://emby:8096")
    db_session.add(other)
    await db_session.flush()
    db_session.add(MediaServerBinding(
        server_id=other.id, server_path_prefix="/data", volume_id=volume_id,
    ))
    await db_session.commit()


# ---------------------------------------------------------------------------
# WorkCollection: partial unique index on identity-bearing rows
# ---------------------------------------------------------------------------


def test_work_collection_identity_index_shape():
    """No plain table-level UNIQUE on (external_source, external_id) — it would
    let every (…, NULL) duplicate through; the partial unique index covers
    identity-bearing rows only."""
    from sqlalchemy import UniqueConstraint

    table = Base.metadata.tables["work_collections"]
    assert not any(
        isinstance(c, UniqueConstraint)
        and [col.name for col in c.columns] == ["external_source", "external_id"]
        for c in table.constraints
    )
    index = next(i for i in table.indexes if i.name == "uq_work_collections_source_external")
    assert index.unique
    assert str(index.dialect_options["sqlite"]["where"].compile(
        compile_kwargs={"literal_binds": True}
    )) == "external_id IS NOT NULL"
    assert index.dialect_options["postgresql"]["where"] is not None


async def test_work_collection_null_identity_rows_coexist(db_session):
    """Shell collections (series_group, NULL id) and manual collections
    (NULL, NULL) are legitimately plural — only identity-bearing pairs are
    unique."""
    from app.models.work_collection import WorkCollection

    db_session.add_all([
        WorkCollection(title_cn="壳甲", external_source="series_group"),
        WorkCollection(title_cn="壳乙", external_source="series_group"),
        WorkCollection(title_cn="手工甲"),
        WorkCollection(title_cn="手工乙"),
        WorkCollection(
            title_cn="T", external_source="tmdb_collection", external_id="131295"
        ),
        WorkCollection(title_cn="无源甲", external_id="q1"),
    ])
    await db_session.commit()

    # Duplicate identity pair rejected.
    db_session.add(WorkCollection(
        title_cn="重复", external_source="tmdb_collection", external_id="131295"
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    # Residual gap by design: a NULL external_source still counts as distinct
    # in both backends' unique-index NULL semantics; every app creator sets
    # source and id together, so this shape never occurs in practice.
    db_session.add(WorkCollection(title_cn="无源乙", external_id="q1"))
    await db_session.flush()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Channel.required_metadata_fields NOT NULL
# ---------------------------------------------------------------------------


async def test_channel_required_metadata_fields_not_null(db_engine, db_session, sample_channel):
    await db_session.commit()  # make the fixture channel visible to the raw conn
    async with db_engine.begin() as conn:
        info = (await conn.execute(text("PRAGMA table_info(channels)"))).fetchall()
        row = next(r for r in info if r[1] == "required_metadata_fields")
        assert row[3] == 1  # NOT NULL
        with pytest.raises(IntegrityError):
            await conn.execute(
                text("UPDATE channels SET required_metadata_fields = NULL WHERE id = :id"),
                {"id": sample_channel.id},
            )


async def test_channel_required_metadata_fields_model_default(db_session):
    """ORM default keeps new channels non-null without an explicit value."""
    from app.models.channel import Channel
    from app.services.required_fields import normalize_required_fields

    ch = Channel(name="c", url="https://x", field_mapping={})
    db_session.add(ch)
    await db_session.flush()  # Python-side column default fires at INSERT
    assert ch.required_metadata_fields == normalize_required_fields([])


# ---------------------------------------------------------------------------
# JSON columns compile to JSONB on PostgreSQL
# ---------------------------------------------------------------------------


def test_json_columns_compile_to_jsonb_on_postgres():
    """Every ORM JSON column (metadata_cache excluded — owned separately)
    compiles to JSONB under the PostgreSQL dialect and plain JSON under
    SQLite, matching the light-migration ADD COLUMN DDL."""
    from sqlalchemy import JSON
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateTable

    pg = postgresql.dialect()
    lite = sqlite.dialect()
    checked = 0
    for name, table in Base.metadata.tables.items():
        if name == "metadata_cache":
            continue
        json_cols = [c for c in table.columns if isinstance(c.type, JSON)]
        if not json_cols:
            continue
        ddl_pg = str(CreateTable(table).compile(dialect=pg))
        ddl_lite = str(CreateTable(table).compile(dialect=lite))
        assert ddl_pg.count("JSONB") == len(json_cols), name
        assert "JSONB" not in ddl_lite, name
        checked += len(json_cols)
    assert checked > 30  # guards against a silently empty scan


# ---------------------------------------------------------------------------
# Episode composite FK: season == parent season_number
# ---------------------------------------------------------------------------


def test_episode_composite_fk_shape():
    from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

    table = Base.metadata.tables["episodes"]
    # The legacy plain series_id FK is gone; every FK on series_id belongs to
    # the named composite constraint now.
    assert table.c.series_id.foreign_keys
    assert all(
        fk.constraint is not None and fk.constraint.name == "fk_episodes_series_season"
        for fk in table.c.series_id.foreign_keys
    )
    fk = next(
        c for c in table.constraints
        if isinstance(c, ForeignKeyConstraint) and c.name == "fk_episodes_series_season"
    )
    assert [col.name for col in fk.columns] == ["series_id", "season"]
    assert fk.ondelete == "CASCADE"
    assert fk.deferrable and fk.initially == "DEFERRED"

    parent = Base.metadata.tables["tv_series"]
    assert any(
        isinstance(c, UniqueConstraint) and c.name == "uq_tv_series_id_season_number"
        for c in parent.constraints
    )

    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "DEFERRABLE INITIALLY DEFERRED" in ddl


async def test_episode_composite_fk_enforced(db_session):
    """A mismatching Episode row is rejected at commit (INITIALLY DEFERRED);
    matching rows and joint parent+child season corrections commit fine."""
    from app.models.episode import Episode
    from app.models.series import TVSeries

    series = TVSeries(title_cn="某剧 第三季", content_type="tv", season_number=3)
    db_session.add(series)
    await db_session.flush()
    series_id = series.id
    db_session.add(Episode(series_id=series_id, season=3, episode=1))
    await db_session.commit()

    db_session.add(Episode(series_id=series_id, season=1, episode=2))
    with pytest.raises(IntegrityError):
        await db_session.commit()  # deferred FK: checked at COMMIT
    await db_session.rollback()

    # Season correction: parent and children re-tagged in one transaction.
    series = await db_session.get(TVSeries, series_id)
    ep_id = (await db_session.execute(
        select(Episode.id).where(Episode.series_id == series_id)
    )).scalar_one()
    ep = await db_session.get(Episode, ep_id)
    series.season_number = 4
    ep.season = 4
    await db_session.commit()


async def test_episode_composite_fk_cascade_on_series_delete(db_session):
    from sqlalchemy import delete, func

    from app.models.episode import Episode
    from app.models.series import TVSeries

    series = TVSeries(title_cn="某剧", content_type="tv", season_number=2)
    db_session.add(series)
    await db_session.flush()
    db_session.add(Episode(series_id=series.id, season=2, episode=1))
    await db_session.commit()

    await db_session.execute(delete(TVSeries).where(TVSeries.id == series.id))
    await db_session.commit()
    count = await db_session.scalar(
        select(func.count()).select_from(Episode).where(Episode.series_id == series.id)
    )
    assert count == 0


# ---------------------------------------------------------------------------
# PostgreSQL btree unique-key byte budget (app-layer guard)
# ---------------------------------------------------------------------------


def test_assignment_file_path_byte_budget():
    """uq_assignment_resource_path: 全 CJK 的 1024 字符路径会超 PG btree
    单行上限（~2704B），ORM 层按字节预算拒绝而不是让 DB 报错。"""
    from app.models.resource_file_assignment import ResourceFileAssignment

    ResourceFileAssignment(resource_id=_uuid(), file_path="剧" * 600)  # 1800 B
    with pytest.raises(ValueError, match="file_path"):
        ResourceFileAssignment(resource_id=_uuid(), file_path="剧" * 700)  # 2100 B


def test_library_server_path_byte_budget():
    from app.models.library import Library

    Library(name="ok", server_path="/mnt/媒体/" + "剧" * 600)
    with pytest.raises(ValueError, match="server_path"):
        Library(name="bad", server_path="剧" * 640)  # 1920 B > 1900 B budget


def test_check_unique_key_bytes_helper():
    from app.models.guards import MAX_UNIQUE_KEY_BYTES, check_unique_key_bytes

    check_unique_key_bytes("col", None, reserved_bytes=36)  # NULL passes
    check_unique_key_bytes("col", "a" * (MAX_UNIQUE_KEY_BYTES - 36), reserved_bytes=36)
    with pytest.raises(ValueError, match="col"):
        check_unique_key_bytes("col", "a" * (MAX_UNIQUE_KEY_BYTES - 35), reserved_bytes=36)
