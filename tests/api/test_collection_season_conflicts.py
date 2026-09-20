"""Collection season conflicts must return 409 and preserve both identities."""

import pytest
from sqlalchemy import select

from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId


@pytest.mark.parametrize("shell_source", [False, True], ids=["legacy-orphan", "shell"])
async def test_attach_occupied_season_returns_409_without_moving_identities(client, db_session, shell_source):
    target = WorkCollection(title_cn="Synthetic occupied parent", aliases=["target alias"])
    shell = WorkCollection(title_cn="Synthetic source shell", external_source="series_group", aliases=["source alias"])
    db_session.add_all([target, shell])
    await db_session.flush()
    existing = TVSeries(title_cn="Synthetic existing S1", collection_id=target.id, season_number=1)
    incoming = TVSeries(
        title_cn="Synthetic incoming S1", collection_id=shell.id if shell_source else None, season_number=1
    )
    bag = WorkExternalId(
        work_type="collection", work_id=shell.id, source="wikipedia", external_id="wikipedia:zh:900001"
    )
    db_session.add_all([existing, incoming, bag])
    await db_session.commit()
    target_id, source_id, work_id, bag_id = target.id, shell.id, incoming.id, bag.id
    response = await client.post(
        f"/api/v1/collections/{target_id}/works", json={"work_type": "series", "work_id": work_id}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "DUPLICATE_SUBMISSION"
    db_session.expire_all()
    incoming = await db_session.get(TVSeries, work_id)
    assert incoming.collection_id == (source_id if shell_source else None)
    target = await db_session.get(WorkCollection, target_id)
    assert target.aliases == ["target alias"]
    assert await db_session.get(WorkCollection, source_id) is not None
    assert (await db_session.get(WorkExternalId, bag_id)).work_id == source_id
    assert len(list(await db_session.scalars(select(TVSeries)))) == 2


async def test_unrelated_identity_constraint_is_not_reported_as_season_conflict(client, db_session, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from app.api.v1 import collections

    target = WorkCollection(title_cn="Synthetic empty target")
    shell = WorkCollection(title_cn="Synthetic source shell", external_source="series_group")
    db_session.add_all([target, shell])
    await db_session.flush()
    work = TVSeries(title_cn="Synthetic source work", collection_id=shell.id, season_number=1)
    bag = WorkExternalId(
        work_type="collection", work_id=shell.id, source="wikipedia", external_id="wikipedia:zh:900002"
    )
    db_session.add_all([work, bag])
    await db_session.commit()
    target_id, shell_id, work_id, bag_id = target.id, shell.id, work.id, bag.id
    original = collections.try_absorb_shell_collection

    async def duplicate_identity_after_absorption(db, parent, member):
        result = await original(db, parent, member)
        db.add(
            WorkExternalId(
                work_type="collection", work_id=parent.id, source="wikipedia", external_id="wikipedia:zh:900002"
            )
        )
        await db.flush()
        return result

    monkeypatch.setattr(collections, "try_absorb_shell_collection", duplicate_identity_after_absorption)
    with pytest.raises(IntegrityError, match="work_external_ids"):
        await client.post(f"/api/v1/collections/{target_id}/works", json={"work_type": "series", "work_id": work_id})
    db_session.expire_all()
    assert await db_session.get(WorkCollection, shell_id) is not None
    assert (await db_session.get(TVSeries, work_id)).collection_id == shell_id
    assert (await db_session.get(WorkExternalId, bag_id)).work_id == shell_id


async def test_actual_turso_slot_error_after_absorption_returns_409(client, db_session, monkeypatch):
    from app.api.v1 import collections

    target = WorkCollection(title_cn="Synthetic empty target", aliases=["target alias"])
    shell = WorkCollection(title_cn="Synthetic source", external_source="series_group", aliases=["source alias"])
    db_session.add_all([target, shell])
    await db_session.flush()
    work = TVSeries(title_cn="Synthetic incoming", collection_id=shell.id, season_number=1)
    db_session.add(work)
    await db_session.commit()
    target_id, shell_id, work_id = target.id, shell.id, work.id
    original = collections.try_absorb_shell_collection

    async def slot_failure_after_absorption(db, parent, member):
        result = await original(db, parent, member)
        db.add(TVSeries(title_cn="Injected competing slot", collection_id=parent.id, season_number=1))
        await db.flush()
        return result

    monkeypatch.setattr(collections, "try_absorb_shell_collection", slot_failure_after_absorption)
    response = await client.post(
        f"/api/v1/collections/{target_id}/works", json={"work_type": "series", "work_id": work_id}
    )
    assert response.status_code == 409
    db_session.expire_all()
    assert await db_session.get(WorkCollection, shell_id) is not None
    assert (await db_session.get(TVSeries, work_id)).collection_id == shell_id
    assert (await db_session.get(WorkCollection, target_id)).aliases == ["target alias"]
    assert len(list(await db_session.scalars(select(TVSeries)))) == 1
