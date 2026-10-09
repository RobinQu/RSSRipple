"""Synthetic cross-type histories; actual persistence and production merge paths."""
import uuid

import pytest
from sqlalchemy import select

from app.models.episode import Episode
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.metadata_dedup import merge_cross_type_duplicates, rehome_series_as_movie


@pytest.mark.parametrize("path", ["keep_movie", "keep_series", "rehome"])
@pytest.mark.parametrize("case", ["transfer_null", "conflict", "unsupported"])
async def test_cross_type_preserves_manual_intent(db_session, path, case):
    collection = WorkCollection(id=str(uuid.uuid4()), title_cn="合成合集")
    db_session.add(collection)
    await db_session.flush()
    series = TVSeries(id=str(uuid.uuid4()), title_en="Synthetic cross work", collection_id=collection.id)
    movie = Movie(id=str(uuid.uuid4()), title_en="Synthetic cross work")
    # Cross-type pairing now requires evidence beyond a shared title (equal
    # dates or an identity overlap) — give the pair a shared identity so the
    # merge machinery is exercised.
    series.external_source = movie.external_source = "tmdb"
    series.external_id = movie.external_id = "tmdb:9001"
    source, target = (movie, series) if path == "keep_series" else (series, movie)
    if case == "unsupported":
        name = "runtime" if path == "keep_series" else "number_of_episodes"
        setattr(source, name, 12)
        source.manually_edited_fields = [name]
    else:
        source.description = None
        source.manually_edited_fields = ["description"]
        target.description = "synthetic description"
        if case == "conflict":
            target.manually_edited_fields = ["description"]
    db_session.add_all([series, movie])
    await db_session.flush()
    if path == "keep_series":
        db_session.add(Episode(series_id=series.id, season=1, episode=1))
    await db_session.commit()
    source_id, target_id = source.id, target.id
    if path == "rehome":
        if case == "transfer_null":
            await rehome_series_as_movie(db_session, series, movie)
        else:
            with pytest.raises(ValueError, match="manual"):
                await rehome_series_as_movie(db_session, series, movie)
    else:
        report = await merge_cross_type_duplicates(db_session)
        assert report.cross_type_merges == (1 if case == "transfer_null" else 0)
    await db_session.commit()
    db_session.expire_all()
    actual = await db_session.get(type(target), target_id)
    if case == "transfer_null":
        assert actual.description is None
        assert "description" in actual.manually_edited_fields
        assert (await db_session.execute(select(type(source)))).scalars().all() == []
    else:
        assert await db_session.get(type(source), source_id) is not None
        assert actual.description == target.description
