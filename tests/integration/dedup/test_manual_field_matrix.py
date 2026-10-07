"""Every editable field survives real same-type merge transactions on both DBs.

Values and duplicate/edit history are synthetic; captured graph coverage lives
in test_captured_assignments. Explicit survivor selection exercises user choice
without assuming differently curated titles still form an automatic group.
"""
from datetime import date

import pytest

from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.metadata_dedup import (
    DedupConflictError,
    DedupReport,
    _merge_movie_group,
    _merge_series_group,
)
from app.services.metadata_service import MANUAL_EDITABLE_FIELDS

VALUES = {
    "title_cn": "合成人工标题", "title_en": "Synthetic curated title",
    "original_title": "Synthetic original", "aliases": ["Synthetic curated alias"],
    "description": "Synthetic curator description", "poster_url": "https://example.invalid/curated.jpg",
    "rating": 0.0, "genre": ["Animation"], "status": "Ended", "is_anime": False,
    "number_of_episodes": 0, "start_date": date(2001, 1, 1), "end_date": date(2001, 2, 1),
    "release_date": date(2001, 1, 1), "runtime": 0, "content_type": "movie",
    "external_id": "wikipedia:en:777777", "external_source": "bangumi",
}
assert set(VALUES) == MANUAL_EDITABLE_FIELDS
CASES = [
    (kind, field, state)
    for kind, model in [("movie", Movie), ("series", TVSeries)]
    for field in sorted(VALUES) if hasattr(model, field)
    for state in (["value", "null", "conflict", "empty"]
                  if isinstance(VALUES[field], (str, list)) and field not in {"content_type", "external_id", "external_source"}
                  else ["value", "null", "conflict"])
]


@pytest.mark.parametrize("kind,field,state", CASES)
async def test_manual_field_postgres(dedup_postgres, kind, field, state):
    await _matrix(dedup_postgres, kind, field, state)


@pytest.mark.parametrize("kind,field,state", CASES)
async def test_manual_field_turso(dedup_turso, kind, field, state):
    await _matrix(dedup_turso, kind, field, state)


async def _matrix(database, kind, field, state):
    _, factory = database
    model = Movie if kind == "movie" else TVSeries
    value = ("movie" if kind == "movie" else "tv") if field == "content_type" else VALUES[field]
    manual_value = None if state in {"null", "conflict"} else value
    if state == "empty":
        manual_value = [] if isinstance(value, list) else ""
    async with factory() as seed:
        rows = [model(title_en="Synthetic matrix", external_source="wikipedia",
                      external_id=f"wikipedia:en:{111111 + index}") for index in range(2)]
        if kind == "series":
            for row in rows:
                collection = WorkCollection(title_cn="合成字段矩阵合集")
                seed.add(collection)
                await seed.flush()
                row.collection_id = collection.id
                row.season_number = 1
        target, source = rows
        setattr(target, field, value)
        setattr(source, field, manual_value)
        if field == "external_source":
            target.external_id = "bangumi:111111"
            if manual_value == "bangumi":
                source.external_id = "bangumi:777777"
        source.manually_edited_fields = [field]
        if state == "conflict":
            target.manually_edited_fields = [field]
        seed.add_all(rows)
        await seed.commit()
        target_id, source_id = target.id, source.id
    merge = _merge_movie_group if kind == "movie" else _merge_series_group
    async with factory() as db:
        target, source = [await db.get(model, identity) for identity in [target_id, source_id]]
        if state == "conflict":
            with pytest.raises(DedupConflictError) as error:
                await merge(db, [target, source], DedupReport(), survivor=target)
            assert field in error.value.fields
            await db.rollback()
        else:
            await merge(db, [target, source], DedupReport(), survivor=target)
            await db.commit()
    async with factory() as check:
        target, source = [await check.get(model, identity) for identity in [target_id, source_id]]
        if state == "conflict":
            assert getattr(target, field) == value
            assert source is not None and getattr(source, field) is None
            assert target.manually_edited_fields == source.manually_edited_fields == [field]
        else:
            assert source is None
            assert getattr(target, field) == manual_value
            assert target.manually_edited_fields == [field]
