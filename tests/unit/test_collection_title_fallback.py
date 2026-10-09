"""``_find_collection_by_titles`` multi-match determinism: a reservation shell
never steals a title match from the populated IP collection (backend row
order must not decide).
"""

from __future__ import annotations

import uuid

from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.metadata_service import _find_collection_by_titles


def _uuid() -> str:
    return str(uuid.uuid4())


async def test_multi_match_prefers_populated_collection(db_session):
    """Shell (exact title, no members) vs main collection (base alias, with
    works): the populated collection wins regardless of row order."""
    shell = WorkCollection(
        id=_uuid(), title_cn="某作品 Final Stage", title_en="某作品 Final Stage",
        external_source="series_group",
    )
    main = WorkCollection(
        id=_uuid(), title_cn="某作品 第一季", external_source="series_group",
        aliases=["某作品"],
    )
    member = TVSeries(
        id=_uuid(), title_cn="某作品", content_type="tv", season_number=1,
        collection_id=main.id,
    )
    db_session.add_all([shell, main, member])
    await db_session.flush()

    found = await _find_collection_by_titles(db_session, ["某作品", "某作品 Final Stage"])
    assert found is not None and found.id == main.id


async def test_multi_match_movie_members_count_too(db_session):
    shell = WorkCollection(
        id=_uuid(), title_cn="某电影 导演剪辑版", external_source="series_group",
    )
    main = WorkCollection(
        id=_uuid(), title_cn="某电影合集", external_source="series_group",
        aliases=["某电影 导演剪辑版"],
    )
    movie = Movie(
        id=_uuid(), title_cn="某电影", content_type="movie", collection_id=main.id,
    )
    db_session.add_all([shell, main, movie])
    await db_session.flush()

    found = await _find_collection_by_titles(db_session, ["某电影 导演剪辑版"])
    assert found is not None and found.id == main.id


async def test_multi_match_all_empty_shells_is_deterministic(db_session):
    """No members anywhere: the oldest row (then id) wins — deterministic
    across backends, independent of storage row order."""
    a = WorkCollection(id=_uuid(), title_cn="壳甲", external_source="series_group", aliases=["同名作品"])
    b = WorkCollection(id=_uuid(), title_cn="壳乙", external_source="series_group", aliases=["同名作品"])
    db_session.add_all([a, b])
    await db_session.flush()

    first = await _find_collection_by_titles(db_session, ["同名作品"])
    second = await _find_collection_by_titles(db_session, ["同名作品"])
    assert first is not None and first.id == second.id
    assert first.id in {a.id, b.id}


async def test_single_or_no_match_unchanged(db_session):
    only = WorkCollection(id=_uuid(), title_cn="唯一合集", external_source="series_group")
    db_session.add(only)
    await db_session.flush()
    found = await _find_collection_by_titles(db_session, ["唯一合集"])
    assert found is not None and found.id == only.id
    assert await _find_collection_by_titles(db_session, ["不存在"]) is None
    assert await _find_collection_by_titles(db_session, []) is None
