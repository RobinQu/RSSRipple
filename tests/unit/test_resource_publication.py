"""Publication prototype: real database transactions and explicit synthetic rows."""

import uuid

import pytest
from sqlalchemy import select

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.resource_publication import ChannelPublicationCounter, ResourcePublication
from app.services.resource_publication import publish_resource


async def resource(db):
    channel = Channel(
        name="Synthetic publication", url="https://example.invalid/" + str(uuid.uuid4()), field_mapping={}
    )
    db.add(channel)
    await db.flush()
    row = FileResource(channel_id=channel.id, guid=str(uuid.uuid4()), title_raw="Synthetic", torrent_url="synthetic")
    db.add(row)
    await db.flush()
    return row


async def test_metadata_event_retains_original_admission_sequence(db_session):
    row = await resource(db_session)
    created = await publish_resource(db_session, row.id, kind="created")
    metadata = await publish_resource(db_session, row.id, kind="metadata")
    assert (created.sequence, metadata.sequence, metadata.origin_sequence) == (1, 2, 1)


async def test_rollback_does_not_advance_published_prefix(db_session):
    row = await resource(db_session)
    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            await publish_resource(db_session, row.id, kind="created")
            raise RuntimeError("Synthetic abort")
    assert not list(await db_session.scalars(select(ResourcePublication)))
    assert not list(await db_session.scalars(select(ChannelPublicationCounter)))
    event = await publish_resource(db_session, row.id, kind="created")
    assert event.sequence == 1


@pytest.mark.parametrize("kind", ["metadata", "unknown"])
async def test_invalid_initial_publication_rejects_and_rolls_back(db_session, kind):
    row = await resource(db_session)
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await publish_resource(db_session, row.id, kind=kind)
    assert not list(await db_session.scalars(select(ResourcePublication)))
    assert not list(await db_session.scalars(select(ChannelPublicationCounter)))


async def test_metadata_retention_is_bounded_and_rollback_preserves_last_wakeup(db_session):
    row = await resource(db_session)
    await publish_resource(db_session, row.id, kind="created")
    for _ in range(10):
        latest = await publish_resource(db_session, row.id, kind="metadata")
    events = list(await db_session.scalars(select(ResourcePublication).order_by(ResourcePublication.sequence)))
    assert [(e.kind, e.sequence, e.origin_sequence) for e in events] == [("created", 1, 1), ("metadata", 11, 1)]
    last_id = latest.id
    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            await publish_resource(db_session, row.id, kind="metadata")
            raise RuntimeError("Synthetic failed refresh transaction")
    kept = await db_session.scalar(select(ResourcePublication).where(ResourcePublication.kind == "metadata"))
    assert kept.id == last_id and kept.sequence == 11
    assert await db_session.scalar(select(ChannelPublicationCounter.sequence)) == 11
