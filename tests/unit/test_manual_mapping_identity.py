"""Fixed-season identity checks use synthetic IDs and real database commits."""

import pytest
from sqlalchemy import select

from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from app.services.metadata_service import MetadataTargetMismatchError, create_or_update_series_from_external


@pytest.mark.parametrize("identity_route", ["season_bag", "collection_bag", "legacy_primary"])
@pytest.mark.parametrize("matches_target", [False, True])
async def test_fixed_target_covers_external_identity_routes(db_session, identity_route, matches_target):
    collections = [WorkCollection(title_cn=f"Synthetic identity collection {i}") for i in range(2)]
    db_session.add_all(collections)
    await db_session.flush()
    works = [TVSeries(title_cn=f"Synthetic season {i}", season_number=2, collection_id=c.id)
             for i, c in enumerate(collections)]
    db_session.add_all(works)
    await db_session.flush()
    target = works[0]
    selected = works[0] if matches_target else works[1]
    source = "bangumi" if identity_route == "season_bag" else "tmdb"
    external_id = f"{source}:90000321"
    if identity_route == "legacy_primary":
        selected.external_source = source
        selected.external_id = external_id
    else:
        db_session.add(WorkExternalId(
            work_type="collection" if identity_route == "collection_bag" else "series",
            work_id=selected.collection_id if identity_route == "collection_bag" else selected.id,
            source=source, external_id=external_id,
        ))
    await db_session.commit()
    before = [(r.work_type, r.work_id, r.source, r.external_id)
              for r in (await db_session.execute(select(WorkExternalId))).scalars()]
    ids = [w.id for w in works]
    data = {"title_cn": "Incoming synthetic title", "external_source": source,
            "external_id": external_id, "content_type": "tv", "description": "Incoming description"}
    if matches_target:
        result = await create_or_update_series_from_external(
            db_session, data, season_hint=2, expected_series_id=target.id,
        )
        assert result.id == target.id
    else:
        with pytest.raises(MetadataTargetMismatchError):
            await create_or_update_series_from_external(
                db_session, data, season_hint=2, expected_series_id=target.id,
            )
    await db_session.commit()
    from app.database import async_session_factory

    async with async_session_factory() as observer:
        for index, work_id in enumerate(ids):
            saved = await observer.get(TVSeries, work_id)
            if matches_target and index == 0:
                assert saved.description == "Incoming description"
            else:
                assert saved.description is None
                assert saved.title_cn == f"Synthetic season {index}"
        if not matches_target:
            after = [(r.work_type, r.work_id, r.source, r.external_id)
                     for r in (await observer.execute(select(WorkExternalId))).scalars()]
            assert sorted(after) == sorted(before)
