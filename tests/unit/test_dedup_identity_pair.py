"""Primary identity changes must retain a recorded source/id pair and old aliases."""
import pytest
from sqlalchemy import select

from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from app.services.metadata_dedup import (
    DedupReport,
    _merge_movie_group,
    _merge_series_group,
    merge_cross_type_duplicates,
)


@pytest.mark.parametrize("kind", ["movie", "series"])
@pytest.mark.parametrize("manual", [False, True])
async def test_identity_pair_and_original_bag_survive(db_session, kind, manual):
    model = Movie if kind == "movie" else TVSeries
    rows = []
    for source, identity in [('wikipedia', 'wikipedia:en:12345'), ('tmdb', 'tmdb:12345')]:
        attrs = dict(title_en="Synthetic identity pair", external_source=source, external_id=identity)
        if kind == 'series':
            collection = WorkCollection(title_cn="合成身份合集")
            db_session.add(collection)
            await db_session.flush()
            attrs.update(collection_id=collection.id, season_number=1)
        rows.append(model(**attrs))
    target, duplicate = rows
    if manual:
        duplicate.manually_edited_fields = ['external_id']
    db_session.add_all(rows)
    await db_session.commit()
    target_id = target.id
    merge = _merge_movie_group if kind == 'movie' else _merge_series_group
    await merge(db_session, rows, DedupReport(), survivor=target)
    await db_session.commit()
    db_session.expire_all()
    target = await db_session.get(model, target_id)
    assert (target.external_source, target.external_id) == (
        ('tmdb', 'tmdb:12345') if manual else ('wikipedia', 'wikipedia:en:12345')
    )
    if manual:
        assert target.manually_edited_fields == ['external_id']
        bag = (await db_session.execute(select(WorkExternalId).where(
            WorkExternalId.work_id == target_id,
        ))).scalars().all()
        assert ('wikipedia', 'wikipedia:en:12345') in {(row.source, row.external_id) for row in bag}


async def test_incompatible_protected_content_type_does_not_cross_tables(db_session):
    series = TVSeries(title_en="Synthetic manual type", content_type='tv', manually_edited_fields=['content_type'])
    movie = Movie(title_en="Synthetic manual type", content_type='movie')
    db_session.add_all([series, movie])
    await db_session.commit()
    report = await merge_cross_type_duplicates(db_session)
    await db_session.commit()
    assert report.cross_type_merges == 0
    assert await db_session.get(TVSeries, series.id) is not None
    assert movie.content_type == 'movie'
