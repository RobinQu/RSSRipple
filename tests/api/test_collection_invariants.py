"""Production API and real database: every season work retains a collection."""

from sqlalchemy import select

from app.models.series import TVSeries
from app.models.work_collection import WorkCollection


async def test_manual_series_creation_has_collection(client, db_session_factory):
    response = await client.post("/api/v1/series", json={"title_en": "Synthetic Season Work"})
    assert response.status_code == 201
    identity = response.json()["data"]["id"]
    async with db_session_factory() as db:
        series = await db.get(TVSeries, identity)
        assert series.collection_id is not None
        collection = await db.get(WorkCollection, series.collection_id)
        assert collection is not None
        assert collection.external_source == "series_group"


async def test_delete_collection_does_not_orphan_member(client, db_session_factory):
    collection_response = await client.post("/api/v1/collections", json={"title_cn": "合成合集"})
    assert collection_response.status_code == 201
    old_id = collection_response.json()["data"]["id"]
    series_response = await client.post("/api/v1/series", json={"title_en": "Synthetic Member"})
    assert series_response.status_code == 201
    series_id = series_response.json()["data"]["id"]
    attached = await client.post(
        f"/api/v1/collections/{old_id}/works",
        json={
            "work_type": "series",
            "work_id": series_id,
        },
    )
    assert attached.status_code == 201
    response = await client.delete(f"/api/v1/collections/{old_id}")
    assert response.status_code == 200
    async with db_session_factory() as db:
        assert await db.get(WorkCollection, old_id) is None
        member = await db.get(TVSeries, series_id)
        assert member.collection_id is not None
        assert member.collection_id != old_id
        assert await db.get(WorkCollection, member.collection_id) is not None
        assert not (await db.scalars(select(TVSeries).where(TVSeries.collection_id.is_(None)))).all()


async def test_detach_series_keeps_a_shell_collection(client, db_session_factory):
    response = await client.post("/api/v1/collections", json={"title_cn": "待拆分合集"})
    assert response.status_code == 201
    old_id = response.json()["data"]["id"]
    response = await client.post("/api/v1/series", json={"title_en": "Synthetic Detached Season"})
    assert response.status_code == 201
    series_id = response.json()["data"]["id"]
    attached = await client.post(
        f"/api/v1/collections/{old_id}/works",
        json={
            "work_type": "series",
            "work_id": series_id,
        },
    )
    assert attached.status_code == 201
    response = await client.delete(f"/api/v1/collections/{old_id}/works/{series_id}", params={"work_type": "series"})
    assert response.status_code == 200
    async with db_session_factory() as db:
        member = await db.get(TVSeries, series_id)
        assert member.collection_id is not None
        assert member.collection_id != old_id
        assert await db.get(WorkCollection, member.collection_id) is not None
        assert await db.get(WorkCollection, old_id) is not None


async def test_delete_collection_removes_its_identity_bag(client, db_session_factory):
    from app.models.work_external_id import WorkExternalId

    response = await client.post("/api/v1/collections", json={"title_cn": "身份合集"})
    assert response.status_code == 201
    old_id = response.json()["data"]["id"]
    async with db_session_factory() as db:
        db.add(
            WorkExternalId(
                work_type="collection", work_id=old_id, source="wikipedia", external_id="wikipedia:en:900007"
            )
        )
        await db.commit()
    response = await client.delete(f"/api/v1/collections/{old_id}")
    assert response.status_code == 200
    async with db_session_factory() as db:
        assert await db.get(WorkCollection, old_id) is None
        assert not (
            await db.scalars(
                select(WorkExternalId).where(
                    WorkExternalId.work_type == "collection",
                    WorkExternalId.work_id == old_id,
                )
            )
        ).all()


async def test_delete_collection_preserves_manual_resource_links(client, db_session_factory, sample_channel):
    import uuid

    from app.models.file_resource import FileResource
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.models.resource_work_link import ResourceWorkLink

    response = await client.post("/api/v1/collections", json={"title_cn": "带资源合集"})
    assert response.status_code == 201
    old_id = response.json()["data"]["id"]
    response = await client.post("/api/v1/series", json={"title_en": "Synthetic Linked Season"})
    assert response.status_code == 201
    series_id = response.json()["data"]["id"]
    attached = await client.post(
        f"/api/v1/collections/{old_id}/works",
        json={
            "work_type": "series",
            "work_id": series_id,
        },
    )
    assert attached.status_code == 201
    resource_id = str(uuid.uuid4())
    async with db_session_factory() as db:
        db.add(
            FileResource(
                id=resource_id,
                channel_id=sample_channel.id,
                guid=resource_id,
                title_raw="Synthetic season batch",
                torrent_url="https://example.invalid/test.torrent",
                collection_id=old_id,
                is_batch=True,
                batch_scope="season",
            )
        )
        await db.flush()
        db.add(ResourceWorkLink(resource_id=resource_id, series_id=series_id, source="manual"))
        db.add(
            ResourceFileAssignment(
                resource_id=resource_id,
                file_path="episode01.mkv",
                series_id=series_id,
                season=1,
                episode_start=1,
                episode_end=1,
                source="manual",
            )
        )
        await db.commit()
    response = await client.delete(f"/api/v1/collections/{old_id}")
    assert response.status_code == 200
    async with db_session_factory() as db:
        member = await db.get(TVSeries, series_id)
        resource = await db.get(FileResource, resource_id)
        assert member.collection_id is not None
        assert resource.collection_id == member.collection_id
        links = (await db.scalars(select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource_id))).all()
        assignments = (
            await db.scalars(select(ResourceFileAssignment).where(ResourceFileAssignment.resource_id == resource_id))
        ).all()
        assert [(r.series_id, r.source) for r in links] == [(series_id, "manual")]
        assert [(r.series_id, r.file_path, r.source) for r in assignments] == [(series_id, "episode01.mkv", "manual")]


async def test_multi_work_resource_does_not_guess_new_collection(client, db_session_factory, sample_channel):
    import uuid

    from app.models.file_resource import FileResource
    from app.models.resource_work_link import ResourceWorkLink

    response = await client.post("/api/v1/collections", json={"title_cn": "多作品合集"})
    assert response.status_code == 201
    old_id = response.json()["data"]["id"]
    identities = []
    for season, title in enumerate(["Synthetic One", "Synthetic Two"], 1):
        response = await client.post("/api/v1/series", json={"title_en": title})
        assert response.status_code == 201
        identity = response.json()["data"]["id"]
        identities.append(identity)
        async with db_session_factory() as db:
            member = await db.get(TVSeries, identity)
            member.season_number = season
            await db.commit()
        response = await client.post(
            f"/api/v1/collections/{old_id}/works",
            json={
                "work_type": "series",
                "work_id": identity,
            },
        )
        assert response.status_code == 201
    resource_id = str(uuid.uuid4())
    async with db_session_factory() as db:
        db.add(
            FileResource(
                id=resource_id,
                channel_id=sample_channel.id,
                guid=resource_id,
                title_raw="Synthetic multiple works",
                torrent_url="https://example.invalid/multi.torrent",
                collection_id=old_id,
                is_batch=True,
                batch_scope="multi_season",
            )
        )
        await db.flush()
        for identity in identities:
            db.add(ResourceWorkLink(resource_id=resource_id, series_id=identity, source="manual"))
        await db.commit()
    response = await client.delete(f"/api/v1/collections/{old_id}")
    assert response.status_code == 200
    async with db_session_factory() as db:
        parents = list(await db.scalars(select(TVSeries.collection_id).where(TVSeries.id.in_(identities))))
        assert len(set(parents)) == 2 and None not in parents
        resource = await db.get(FileResource, resource_id)
        assert resource.collection_id is None
        links = (await db.scalars(select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource_id))).all()
        assert {link.series_id for link in links} == set(identities)
        assert all(link.source == "manual" for link in links)


async def test_delete_rolls_back_rehoming_on_cleanup_failure(client, db_session_factory, monkeypatch):
    import pytest

    from app.services import collection_lifecycle

    response = await client.post("/api/v1/collections", json={"title_cn": "事务合集"})
    assert response.status_code == 201
    old_id = response.json()["data"]["id"]
    response = await client.post("/api/v1/series", json={"title_en": "Synthetic Rollback Season"})
    assert response.status_code == 201
    identity = response.json()["data"]["id"]
    response = await client.post(
        f"/api/v1/collections/{old_id}/works",
        json={
            "work_type": "series",
            "work_id": identity,
        },
    )
    assert response.status_code == 201
    async with db_session_factory() as db:
        before = set(await db.scalars(select(WorkCollection.id)))

    async def fail_cleanup(db, *args):
        # Verify the mutation really happened before simulating downstream failure.
        member = await db.get(TVSeries, identity)
        assert member.collection_id != old_id
        raise RuntimeError("injected collection cleanup failure")

    monkeypatch.setattr(collection_lifecycle, "delete_external_ids_for_work", fail_cleanup)
    with pytest.raises(RuntimeError, match="injected collection cleanup failure"):
        await client.delete(f"/api/v1/collections/{old_id}")
    async with db_session_factory() as db:
        assert set(await db.scalars(select(WorkCollection.id))) == before
        member = await db.get(TVSeries, identity)
        assert member.collection_id == old_id


async def test_orphan_backfill_is_idempotent_and_reconciles_resources(db_session_factory, monkeypatch, sample_channel):
    import uuid

    from app.models.file_resource import FileResource
    from app.services.collection_lifecycle import backfill_orphan_collections

    monkeypatch.setattr("app.database.async_session_factory", db_session_factory)
    identity = str(uuid.uuid4())
    resource_id = str(uuid.uuid4())
    async with db_session_factory() as db:
        db.add(TVSeries(id=identity, title_en="Synthetic Legacy Orphan", season_number=2))
        await db.flush()
        db.add(
            FileResource(
                id=resource_id,
                channel_id=sample_channel.id,
                guid=resource_id,
                title_raw="Synthetic S02E01",
                torrent_url="https://example.invalid/legacy.torrent",
                series_id=identity,
                season=2,
                episode=1,
            )
        )
        await db.commit()
    assert await backfill_orphan_collections() == 1
    async with db_session_factory() as db:
        member = await db.get(TVSeries, identity)
        parent = member.collection_id
        assert parent is not None
        resource = await db.get(FileResource, resource_id)
        assert resource.collection_id == parent
        assert member.season_number == resource.season == 2
    assert await backfill_orphan_collections() == 0
    async with db_session_factory() as db:
        assert (await db.get(TVSeries, identity)).collection_id == parent
        assert len(list(await db.scalars(select(WorkCollection.id)))) == 1


async def test_remove_preloaded_collection_does_not_null_rehomed_members(db_session_factory):
    from sqlalchemy.orm import selectinload

    from app.services.collection_lifecycle import remove_collection

    async with db_session_factory() as db:
        collection = WorkCollection(title_cn='预加载合集')
        db.add(collection)
        await db.flush()
        member = TVSeries(title_en='Synthetic Preloaded Member', collection_id=collection.id)
        db.add(member)
        await db.commit()
        collection_id, member_id = collection.id, member.id
    async with db_session_factory() as db:
        collection = await db.scalar(select(WorkCollection).where(WorkCollection.id == collection_id)
                                     .options(selectinload(WorkCollection.series)))
        assert [member.id for member in collection.series] == [member_id]
        await remove_collection(db, collection)
        await db.commit()
    async with db_session_factory() as db:
        member = await db.get(TVSeries, member_id)
        assert member.collection_id is not None
        assert member.collection_id != collection_id
        assert await db.get(WorkCollection, collection_id) is None
