"""Synthetic concurrent writes after merge locks, using actual database sessions."""
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.database import _is_retryable_lock_error
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_work_link import ResourceWorkLink
from app.services import metadata_dedup as dedup


@pytest.mark.parametrize("action", ["manual_edit", "new_reference", "wizard_reference", "delete"])
async def test_late_writer_postgres(dedup_postgres, monkeypatch, action):
    await _late_writer(dedup_postgres, monkeypatch, action)


@pytest.mark.parametrize("action", ["manual_edit", "new_reference", "wizard_reference", "delete"])
async def test_late_writer_turso(dedup_turso, monkeypatch, action):
    await _late_writer(dedup_turso, monkeypatch, action)


async def _late_writer(database, monkeypatch, action):
    engine, factory = database
    postgres = engine.dialect.name == "postgresql"
    async with factory() as seed:
        channel = Channel(name="Synthetic late writer", type="rss_feed", url="https://example.invalid", field_mapping={})
        target, source = [Movie(title_en="Synthetic late merge", description="automatic") for _ in range(2)]
        seed.add_all([channel, target, source])
        await seed.flush()
        resource = FileResource(channel_id=channel.id, guid="synthetic", title_raw="Synthetic",
                                torrent_url="https://example.invalid/test.torrent")
        seed.add(resource)
        await seed.commit()
        target_id, source_id, resource_id = target.id, source.id, resource.id
    original = dedup.lock_merge_works
    attempted = committed = False

    async def late_writer(db, rows):
        nonlocal attempted, committed
        await original(db, rows)
        if attempted:
            return
        attempted = True
        async with factory() as writer:
            if postgres:
                # Bound a genuine blocking lock acquisition; no mocked lock or sleep.
                await writer.execute(text("SET LOCAL lock_timeout = '250ms'"))
            try:
                row = await writer.get(Movie, source_id)
                if action == "manual_edit":
                    row.description = "late curator value"
                    row.manually_edited_fields = ["description"]
                elif action == "delete":
                    await writer.delete(row)
                elif action == "wizard_reference":
                    from app.schemas.file_resource import ResourceAssociationUpdateRequest
                    from app.services.resource_association import apply_association_update

                    resource = await writer.get(FileResource, resource_id)
                    body = ResourceAssociationUpdateRequest(
                        is_batch=True, works=[{"work_type": "movie", "work_id": source_id}],
                    )
                    await apply_association_update(writer, resource, body)
                else:
                    writer.add(ResourceWorkLink(resource_id=resource_id, movie_id=source_id, source="manual"))
                await writer.commit()
                committed = True
            except DBAPIError as error:
                if postgres:
                    assert getattr(error.orig, "sqlstate", None) == "55P03"
                else:
                    assert _is_retryable_lock_error(error)
                await writer.rollback()

    monkeypatch.setattr(dedup, "lock_merge_works", late_writer)
    conflicted = False
    async with factory() as merger:
        target, source = [await merger.get(Movie, identity) for identity in [target_id, source_id]]
        try:
            await dedup._merge_movie_group(merger, [target, source], dedup.DedupReport(), survivor=target)
            await merger.commit()
        except DBAPIError as error:
            assert _is_retryable_lock_error(error)
            conflicted = True
            await merger.rollback()
    assert attempted
    if postgres:
        assert not committed and not conflicted
    if conflicted and action != "delete":
        async with factory() as retry:
            target, source = [await retry.get(Movie, identity) for identity in [target_id, source_id]]
            await dedup._merge_movie_group(retry, [target, source], dedup.DedupReport(), survivor=target)
            await retry.commit()
    async with factory() as check:
        target = await check.get(Movie, target_id)
        source = await check.get(Movie, source_id)
        assert target is not None
        if committed and action == "manual_edit":
            assert target.description == "late curator value"
            assert target.manually_edited_fields == ["description"]
        if committed and action in {"new_reference", "wizard_reference"}:
            link = (await check.scalars(select(ResourceWorkLink))).one()
            assert link.movie_id == target_id and link.source == "manual"
        assert source is None
