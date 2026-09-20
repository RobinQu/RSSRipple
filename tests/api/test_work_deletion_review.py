"""Necessity review: real API/DB, explicitly synthetic work associations."""

import uuid

import pytest
from sqlalchemy import select

from app.models.file_resource import FileResource
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from app.services.external_ids import add_external_id


@pytest.mark.parametrize("kind", ["series", "movie"])
async def test_deleted_work_releases_external_identity(client, db_session_factory, sample_series, sample_movie, kind):
    work = sample_series if kind == "series" else sample_movie
    async with db_session_factory() as db:
        assert await add_external_id(db, kind, work.id, "tmdb", "987654321")
        await db.commit()
    route = "series" if kind == "series" else "movies"
    response = await client.delete(f"/api/v1/{route}/{work.id}")
    assert response.status_code == 200, response.text
    async with db_session_factory() as db:
        leftovers = list(
            await db.scalars(
                select(WorkExternalId).where(
                    WorkExternalId.work_type == kind,
                    WorkExternalId.work_id == work.id,
                )
            )
        )
        replacement = type(work)(title_cn="Synthetic replacement")
        if kind == "series":
            collection = WorkCollection(title_cn="Synthetic replacement collection")
            db.add(collection)
            await db.flush()
            replacement.collection_id = collection.id
            replacement.season_number = 1
        db.add(replacement)
        await db.flush()
        registered = await add_external_id(db, kind, replacement.id, "tmdb", "987654321")
        assert not leftovers and registered, {
            "leftover_identity_rows": len(leftovers),
            "replacement_registered": registered,
        }


@pytest.mark.parametrize("kind", ["series", "movie"])
async def test_manual_file_mapping_blocks_unreviewed_work_delete(
    client,
    db_session_factory,
    sample_channel,
    sample_series,
    sample_movie,
    kind,
):
    work = sample_series if kind == "series" else sample_movie
    async with db_session_factory() as db:
        resource = FileResource(
            id=str(uuid.uuid4()),
            channel_id=sample_channel.id,
            guid=str(uuid.uuid4()),
            title_raw="Synthetic manual placement",
            torrent_url="magnet:?xt=urn:btih:synthetic",
        )
        db.add(resource)
        await db.flush()
        link = ResourceWorkLink(resource_id=resource.id, source="manual", **{kind + "_id": work.id})
        assignment = ResourceFileAssignment(
            resource_id=resource.id, file_path="synthetic.mkv", source="manual", **{kind + "_id": work.id}
        )
        db.add_all([link, assignment])
        await db.commit()
        link_id, assignment_id = link.id, assignment.id
    route = "series" if kind == "series" else "movies"
    response = await client.delete(f"/api/v1/{route}/{work.id}")
    async with db_session_factory() as db:
        remaining_link = await db.get(ResourceWorkLink, link_id)
        remaining_assignment = await db.get(ResourceFileAssignment, assignment_id)
        observed = dict(
            status=response.status_code,
            link_retained=remaining_link is not None,
            assignment_retained=remaining_assignment is not None,
        )
        assert response.status_code == 409 and remaining_link and remaining_assignment, observed
    assert response.json()["error"]["code"] == "DELETE_BLOCKED"


@pytest.mark.parametrize("kind", ["series", "movie"])
async def test_identity_cleanup_failure_rolls_back_work_and_resource(
    client,
    db_session_factory,
    sample_channel,
    sample_series,
    sample_movie,
    kind,
    monkeypatch,
):
    import httpx

    import app.services.external_ids as identities
    from tests.api.conftest import _build_test_app

    work = sample_series if kind == "series" else sample_movie
    async with db_session_factory() as db:
        assert await add_external_id(db, kind, work.id, "tmdb", "987654322")
        resource = FileResource(
            channel_id=sample_channel.id,
            guid=str(uuid.uuid4()),
            title_raw="Synthetic rollback reference",
            torrent_url="magnet:?xt=urn:btih:synthetic",
            **{kind + "_id": work.id},
        )
        db.add(resource)
        await db.commit()
        resource_id = resource.id
    real_cleanup = identities.delete_external_ids_for_work

    async def fail_after_cleanup(db, work_type, work_id):
        await real_cleanup(db, work_type, work_id)
        raise RuntimeError("synthetic failure after identity deletion")

    monkeypatch.setattr(identities, "delete_external_ids_for_work", fail_after_cleanup)
    app = _build_test_app(db_session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as api:
        route = "series" if kind == "series" else "movies"
        response = await api.delete(f"/api/v1/{route}/{work.id}")
        assert response.status_code == 500
    async with db_session_factory() as db:
        assert await db.get(type(work), work.id) is not None
        current = await db.get(FileResource, resource_id)
        assert getattr(current, kind + "_id") == work.id
        assert (
            await db.scalar(
                select(WorkExternalId.id).where(
                    WorkExternalId.work_type == kind,
                    WorkExternalId.work_id == work.id,
                )
            )
            is not None
        )


@pytest.mark.parametrize("kind", ["series", "movie"])
@pytest.mark.parametrize("reference", ["work_links", "file_assignments", "title_mappings"])
async def test_each_manual_reference_independently_blocks_delete(
    client, db_session_factory, sample_channel, sample_series, sample_movie, kind, reference
):
    from app.models.channel_raw_title_mapping import ChannelRawTitleMapping

    work = sample_series if kind == "series" else sample_movie
    target = {kind + "_id": work.id}
    async with db_session_factory() as db:
        if reference == "title_mappings":
            row = ChannelRawTitleMapping(
                channel_id=sample_channel.id,
                raw_title="Synthetic manual title",
                search_title_key="synthetic-manual-title",
                **target,
            )
        else:
            resource = FileResource(
                channel_id=sample_channel.id,
                guid=str(uuid.uuid4()),
                title_raw="Synthetic independent reference",
                torrent_url="magnet:?xt=urn:btih:synthetic",
            )
            db.add(resource)
            await db.flush()
            if reference == "work_links":
                row = ResourceWorkLink(resource_id=resource.id, source="manual", **target)
            else:
                row = ResourceFileAssignment(
                    resource_id=resource.id, file_path="synthetic.mkv", source="manual", **target
                )
        db.add(row)
        await db.commit()
        row_id = row.id
    route = "series" if kind == "series" else "movies"
    response = await client.delete(f"/api/v1/{route}/{work.id}")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "DELETE_BLOCKED"
    assert response.json()["error"]["details"] == {"manual_" + reference: 1}
    async with db_session_factory() as db:
        assert await db.get(type(work), work.id) is not None
        retained = await db.get(type(row), row_id)
        assert retained is not None
        assert getattr(retained, kind + "_id") == work.id
