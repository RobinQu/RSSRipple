"""Publish within the resource transaction; never perform network work under this lock."""

import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models.file_resource import FileResource
from app.models.resource_publication import ChannelPublicationCounter, ResourcePublication


async def publish_resource(db, resource_id, *, kind):
    if kind not in {"created", "metadata"}:
        raise ValueError("Unsupported publication kind")
    channel_id = await db.scalar(select(FileResource.channel_id).where(FileResource.id == resource_id))
    if channel_id is None:
        raise ValueError("Resource must exist before publication")
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(ChannelPublicationCounter)
        .values(
            id=str(uuid.uuid4()),
            channel_id=channel_id,
            sequence=0,
        )
        .on_conflict_do_nothing(index_elements=["channel_id"])
    )
    # UPDATE, unlike a sequence generator, serializes allocation until commit.
    sequence = await db.scalar(
        update(ChannelPublicationCounter)
        .where(ChannelPublicationCounter.channel_id == channel_id)
        .values(sequence=ChannelPublicationCounter.sequence + 1)
        .returning(ChannelPublicationCounter.sequence)
    )
    origin = await db.scalar(
        select(ResourcePublication).where(
            ResourcePublication.resource_id == resource_id,
            ResourcePublication.kind == "created",
        )
    )
    if kind == "created" and origin is not None:
        raise ValueError("Resource already published; roll back transaction")
    if kind == "metadata" and origin is None:
        raise ValueError("Resource requires an initial publication; roll back transaction")
    if kind == "metadata":
        # Consumers read the resource's latest committed state, not event
        # payload history. Replace the prior wakeup in this same transaction;
        # an old snapshot can acknowledge only its earlier sequence.
        await db.execute(
            delete(ResourcePublication).where(
                ResourcePublication.resource_id == resource_id,
                ResourcePublication.kind == "metadata",
            )
        )
    publication = ResourcePublication(
        channel_id=channel_id,
        resource_id=resource_id,
        kind=kind,
        sequence=sequence,
        origin_sequence=sequence if kind == "created" else origin.sequence,
    )
    db.add(publication)
    await db.flush()
    return publication
