"""Conservative existing-work links for explicitly declared batch seasons."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.file_resource import FileResource
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.declared_season_links import link_declared_seasons


def _uuid() -> str:
    return str(uuid.uuid4())


def _resource(channel_id: str, series_id: str | None, seasons: list[int]) -> FileResource:
    return FileResource(
        id=_uuid(), channel_id=channel_id, guid=_uuid(),
        title_raw="旋风管家！全四季", torrent_url="magnet:?xt=urn:btih:declared",
        is_batch=True, batch_scope="multi_season", batch_seasons=seasons,
        series_id=series_id,
    )


async def _links(db_session, resource_id: str) -> list[ResourceWorkLink]:
    return list((await db_session.scalars(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource_id)
    )).all())


async def test_flat_s1_links_only_unique_declared_existing_collection_seasons(
    db_session, sample_channel,
):
    collection = WorkCollection(id=_uuid(), title_cn="旋风管家")
    db_session.add(collection)
    await db_session.flush()
    works = {
        season: TVSeries(
            id=_uuid(), title_cn=f"旋风管家 第{season}季",
            collection_id=collection.id, season_number=season,
        )
        for season in (1, 2, 3, 4, 5, 0)
    }
    resource = _resource(sample_channel.id, works[1].id, [1, 2, 3, 4])
    db_session.add_all([*works.values(), resource])
    await db_session.flush()

    assert await link_declared_seasons(db_session, resource) == 4
    await db_session.flush()
    links = await _links(db_session, resource.id)
    assert {link.series_id for link in links} == {
        works[season].id for season in (1, 2, 3, 4)
    }
    assert all(link.source == "auto" for link in links)
    assert works[5].id not in {link.series_id for link in links}
    assert works[0].id not in {link.series_id for link in links}
    assert resource.series_id == works[1].id
    assert resource.collection_id == collection.id
    assert await db_session.scalar(select(func.count(TVSeries.id))) == 6

    assert await link_declared_seasons(db_session, resource) == 0
    await db_session.flush()
    assert len(await _links(db_session, resource.id)) == 4


async def test_declared_season_links_preserve_manual_rows(db_session, sample_channel):
    collection = WorkCollection(id=_uuid(), title_cn="旋风管家")
    db_session.add(collection)
    await db_session.flush()
    first = TVSeries(
        id=_uuid(), title_cn="S1", collection_id=collection.id, season_number=1
    )
    second = TVSeries(
        id=_uuid(), title_cn="S2", collection_id=collection.id, season_number=2
    )
    resource = _resource(sample_channel.id, first.id, [1, 2])
    db_session.add_all([first, second, resource])
    await db_session.flush()
    db_session.add(ResourceWorkLink(
        resource_id=resource.id, series_id=second.id, source="manual"
    ))
    await db_session.flush()

    assert await link_declared_seasons(db_session, resource) == 1
    links = await _links(db_session, resource.id)
    by_series = {link.series_id: link for link in links}
    assert by_series[first.id].source == "auto"
    assert by_series[second.id].source == "manual"
    assert resource.series_id == first.id


async def test_declared_season_links_do_not_guess_without_collection(
    db_session, sample_channel,
):
    collection = WorkCollection(id=_uuid(), title_cn="另一个合集")
    first = TVSeries(id=_uuid(), title_cn="无合集 S1", season_number=1)
    second = TVSeries(
        id=_uuid(), title_cn="OVA 不应按标题匹配",
        collection_id=collection.id, season_number=2,
    )
    resource = _resource(sample_channel.id, first.id, [1, 2])
    db_session.add_all([collection, first, second, resource])
    await db_session.flush()

    assert await link_declared_seasons(db_session, resource) == 0
    assert await _links(db_session, resource.id) == []
    assert resource.collection_id is None


async def test_declared_season_links_do_not_guess_across_ambiguous_collections(
    db_session, sample_channel,
):
    first_collection = WorkCollection(id=_uuid(), title_cn="合集一")
    second_collection = WorkCollection(id=_uuid(), title_cn="合集二")
    db_session.add_all([first_collection, second_collection])
    await db_session.flush()
    first = TVSeries(
        id=_uuid(), title_cn="S1", collection_id=first_collection.id, season_number=1
    )
    second = TVSeries(
        id=_uuid(), title_cn="S2", collection_id=second_collection.id, season_number=2
    )
    third = TVSeries(
        id=_uuid(), title_cn="S3", collection_id=first_collection.id, season_number=3
    )
    resource = _resource(sample_channel.id, first.id, [1, 2, 3])
    db_session.add_all([first, second, third, resource])
    await db_session.flush()
    db_session.add(ResourceWorkLink(
        resource_id=resource.id, series_id=second.id, source="manual"
    ))
    await db_session.flush()

    assert await link_declared_seasons(db_session, resource) == 0
    links = await _links(db_session, resource.id)
    assert [(link.series_id, link.source) for link in links] == [
        (second.id, "manual")
    ]
    assert resource.series_id == first.id


async def test_fetch_metadata_links_declared_seasons_before_graph(
    db_session, sample_channel, monkeypatch,
):
    from unittest.mock import AsyncMock

    from app.services import fetch_service as fetch_service
    from app.services.metadata_concurrency import MetadataConcurrency
    from app.services.resource_publication import publish_resource

    collection = WorkCollection(id=_uuid(), title_cn="旋风管家")
    db_session.add(collection)
    await db_session.flush()
    first = TVSeries(
        id=_uuid(), title_cn="旋风管家 S1",
        collection_id=collection.id, season_number=1,
    )
    second = TVSeries(
        id=_uuid(), title_cn="旋风管家 S2",
        collection_id=collection.id, season_number=2,
    )
    resource = _resource(sample_channel.id, first.id, [1, 2])
    db_session.add_all([first, second, resource])
    await db_session.flush()
    await publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    resource_id = resource.id
    first_id, second_id = first.id, second.id
    collection_id = collection.id

    async def _inspect(db, current, channel):
        return False

    async def _link(db, current, channel):
        return None

    observed = []

    async def _expand(db, current):
        observed.append(set(await db.scalars(
            select(ResourceWorkLink.series_id).where(
                ResourceWorkLink.resource_id == current.id
            )
        )))

    monkeypatch.setattr("app.services.torrent_inspect.maybe_inspect_torrent", _inspect)
    monkeypatch.setattr(fetch_service, "fetch_and_link_metadata", _link)
    monkeypatch.setattr("app.services.bangumi_relations.expand_bangumi_series_graph", _expand)
    monkeypatch.setattr("app.services.cluster_work_binding.bind_hint_clusters", AsyncMock())
    monkeypatch.setattr("app.services.franchise_service.dedupe_resource_movies", AsyncMock())
    monkeypatch.setattr("app.services.magnet_resolve.enqueue_resolution", AsyncMock())

    await fetch_service._process_resource_metadata(
        resource_id, sample_channel.id, MetadataConcurrency(1), force_refresh=True
    )

    assert observed == [{first_id, second_id}]
    db_session.expire_all()
    saved = await db_session.get(FileResource, resource_id)
    assert saved.series_id == first_id
    assert saved.collection_id == collection_id
    assert {link.series_id for link in await _links(db_session, resource_id)} == {
        first_id, second_id
    }


async def test_late_listing_reconcile_links_declared_seasons_and_keeps_s1_files(
    db_session, sample_channel, monkeypatch,
):
    from unittest.mock import AsyncMock

    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.services import resource_listing as listing_service
    from app.services.resource_publication import publish_resource

    collection = WorkCollection(id=_uuid(), title_cn="旋风管家")
    db_session.add(collection)
    await db_session.flush()
    first = TVSeries(
        id=_uuid(), title_cn="旋风管家 S1",
        collection_id=collection.id, season_number=1,
    )
    second = TVSeries(
        id=_uuid(), title_cn="旋风管家 S2",
        collection_id=collection.id, season_number=2,
    )
    resource = _resource(sample_channel.id, first.id, [1, 2])
    db_session.add_all([first, second, resource])
    await db_session.flush()
    await publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    resource_id = resource.id
    first_id, second_id = first.id, second.id
    collection_id = collection.id

    monkeypatch.setattr("app.services.bangumi_relations.expand_bangumi_series_graph", AsyncMock())
    monkeypatch.setattr(
        "app.services.batch_content_analysis.rebind_absolute_season_assignments",
        AsyncMock(),
    )
    monkeypatch.setattr("app.services.cluster_work_binding.bind_hint_clusters", AsyncMock())
    monkeypatch.setattr("app.services.franchise_service.dedupe_resource_movies", AsyncMock())
    monkeypatch.setattr("app.services.agent_resource_requests.wake_agents", AsyncMock())

    result = await listing_service.reconcile_resource(
        resource_id,
        files=[
            {"name": "Hayate/S01/Hayate S01E01.mkv", "size": 100 * 1024 * 1024},
            {"name": "Hayate/S02/Hayate S02E01.mkv", "size": 100 * 1024 * 1024},
        ],
    )

    assert result.files
    db_session.expire_all()
    saved = await db_session.get(FileResource, resource_id)
    assert saved.series_id == first_id
    assert saved.collection_id == collection_id
    assert {link.series_id for link in await _links(db_session, resource_id)} == {
        first_id, second_id
    }
    first_file = await db_session.scalar(select(ResourceFileAssignment).where(
        ResourceFileAssignment.resource_id == resource_id,
        ResourceFileAssignment.file_path == "Hayate/S01/Hayate S01E01.mkv",
    ))
    assert first_file is not None and first_file.series_id == first_id
