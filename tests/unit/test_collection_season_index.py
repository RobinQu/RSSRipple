"""Production startup must enforce the collection season key on real Turso."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database import Base, create_tables
from app.models.episode import Episode
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId


@pytest.mark.parametrize("existing_schema", [False, True], ids=["fresh", "upgrade"])
@pytest.mark.parametrize("season", [0, 1], ids=["specials", "regular"])
async def test_startup_rejects_duplicate_collection_season(db_engine, db_session, existing_schema, season):
    async with db_engine.begin() as conn:
        if existing_schema:
            await conn.execute(text("DROP INDEX IF EXISTS uq_tv_series_collection_season"))
        else:
            await conn.run_sync(Base.metadata.drop_all)
    await create_tables()
    await create_tables()
    collection = WorkCollection(title_cn="Synthetic season key")
    other = WorkCollection(title_cn="Synthetic other collection")
    db_session.add_all([collection, other])
    await db_session.flush()
    # Other seasons and other collections must remain valid. NULL parents
    # represent the legacy schema and are outside the partial index predicate.
    db_session.add_all(
        [
            TVSeries(title_en="Synthetic first", collection_id=collection.id, season_number=season),
            TVSeries(title_en="Synthetic other season", collection_id=collection.id, season_number=season + 1),
            TVSeries(title_en="Synthetic other parent", collection_id=other.id, season_number=season),
            TVSeries(title_en="Synthetic legacy A", collection_id=None, season_number=season),
            TVSeries(title_en="Synthetic legacy B", collection_id=None, season_number=season),
        ]
    )
    await db_session.commit()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(TVSeries(title_en="Synthetic duplicate", collection_id=collection.id, season_number=season))
            await db_session.flush()
    assert len(list(await db_session.scalars(select(TVSeries)))) == 5


async def test_conflicting_upgrade_preserves_all_manually_edited_works(db_engine, db_session):
    async with db_engine.begin() as conn:
        await conn.execute(text("DROP INDEX uq_tv_series_collection_season"))
    collection = WorkCollection(title_cn="Synthetic conflicting legacy collection")
    db_session.add(collection)
    await db_session.flush()
    originals = [
        TVSeries(title_cn=title, collection_id=collection.id, season_number=1, manually_edited_fields=["title_cn"])
        for title in ["Synthetic manually edited A", "Synthetic manually edited B"]
    ]
    db_session.add_all(originals)
    await db_session.commit()
    expected = {(row.id, row.title_cn, row.collection_id, row.season_number) for row in originals}
    # Refuse an unconstrained upgraded schema; never select an arbitrary
    # survivor or move historical works as a side effect of index creation.
    with pytest.raises(IntegrityError):
        await create_tables()
    db_session.expire_all()
    actual = list(await db_session.scalars(select(TVSeries)))
    assert {(row.id, row.title_cn, row.collection_id, row.season_number) for row in actual} == expected
    assert all(row.manually_edited_fields == ["title_cn"] for row in actual)


async def test_populated_upgrade_preserves_work_and_relationships(db_engine, db_session):
    async with db_engine.begin() as conn:
        await conn.execute(text("DROP INDEX uq_tv_series_collection_season"))
    collection = WorkCollection(title_cn="Synthetic populated upgrade")
    db_session.add(collection)
    await db_session.flush()
    work = TVSeries(
        title_cn="Synthetic protected title",
        collection_id=collection.id,
        season_number=3,
        manually_edited_fields=["title_cn"],
    )
    db_session.add(work)
    await db_session.flush()
    episode = Episode(series_id=work.id, season=3, episode=1, title="Synthetic episode")
    identity = WorkExternalId(work_type="series", work_id=work.id, source="tmdb", external_id="tmdb:900001#s3")
    db_session.add_all([episode, identity])
    await db_session.commit()
    keys = (collection.id, work.id, episode.id, identity.id)
    await create_tables()
    await create_tables()
    db_session.expire_all()
    restored = await db_session.get(TVSeries, keys[1])
    assert restored.collection_id == keys[0]
    assert restored.title_cn == "Synthetic protected title"
    assert restored.season_number == 3
    assert restored.manually_edited_fields == ["title_cn"]
    ep = await db_session.get(Episode, keys[2])
    assert (ep.series_id, ep.season, ep.episode) == (keys[1], 3, 1)
    bag = await db_session.get(WorkExternalId, keys[3])
    assert (bag.work_id, bag.external_id) == (keys[1], "tmdb:900001#s3")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(TVSeries(title_cn="Synthetic conflicting insert", collection_id=keys[0], season_number=3))
            await db_session.flush()
