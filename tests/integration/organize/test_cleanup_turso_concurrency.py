"""Real Turso snapshot interleaving; explicit synthetic DB identities."""

import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import retry_on_lock
from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.services.resource_cleanup import cleanup_channel_unresolved_resources
from tests.unit.test_resource_cleanup import _make_resource


@pytest.mark.parametrize("kind", ["work_link", "assignment", "download"])
@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize("snapshot", ["savepoint", "prior_write"])
async def test_cleanup_old_snapshot_preserves_newly_committed_child(
    db_session, snapshot, kind, operation, record_testsuite_property,
):
    channel = Channel(id=str(uuid.uuid4()), name="Synthetic stale snapshot", type="rss_feed",
                      url="https://example.invalid/rss", field_mapping={})
    movie = Movie(id=str(uuid.uuid4()), title_cn="Synthetic movie")
    downloader = DownloaderInstance(id=str(uuid.uuid4()), name="Synthetic downloader", type="transmission",
                                    url="http://example.invalid/rpc", download_dir="/downloads")
    db_session.add_all([channel, movie, downloader])
    await db_session.flush()
    resource = _make_resource(channel.id)
    previous = _make_resource(channel.id, created_days_ago=1)
    db_session.add_all([resource, previous])
    await db_session.flush()
    model = {"work_link": ResourceWorkLink, "assignment": ResourceFileAssignment, "download": DownloadTask}[kind]
    foreign_key = "file_resource_id" if kind == "download" else "resource_id"
    values = {"movie_id": movie.id, "source": "manual"}
    if kind == "assignment":
        values["file_path"] = "synthetic.mkv"
    elif kind == "download":
        values = {"downloader_id": downloader.id, "download_dir": "/downloads"}
    child_id = str(uuid.uuid4())
    if operation == "update":
        db_session.add(model(id=child_id, **{foreign_key: previous.id}, **values))
    await db_session.commit()
    channel_id, resource_id = channel.id, resource.id
    linked_count = select(func.count()).select_from(model).where(getattr(model, foreign_key) == resource_id)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    attempts = 0

    async def clean():
        nonlocal attempts
        attempts += 1
        async with factory() as cleaner:
            if attempts == 1:
                # A plain SELECT does not pin the DBAPI transaction. Use
                # an actual SAVEPOINT to hold an old MVCC snapshot, then
                # publish a child link in a separate committed transaction.
                if snapshot == "savepoint":
                    await cleaner.begin_nested()
                else:
                    await cleaner.execute(update(Channel).where(Channel.id == channel_id).values(name="Earlier write"))
                assert await cleaner.scalar(linked_count) == 0
                async with factory() as writer:
                    if operation == "insert":
                        writer.add(model(id=child_id, **{foreign_key: resource_id}, **values))
                    else:
                        child = await writer.get(model, child_id)
                        setattr(child, foreign_key, resource_id)
                    await writer.commit()
                assert await cleaner.scalar(linked_count) == 0
            deleted = await cleanup_channel_unresolved_resources(cleaner, channel_id, force=True)
            await cleaner.commit()
            return deleted

    # Use the existing whole-operation retry boundary, without retrying a
    # partially failed session. No query/result or DB error is mocked.
    deleted = await retry_on_lock(clean)
    record_testsuite_property("case", f"{snapshot}/{kind}/{operation}")
    record_testsuite_property("cleanup_attempts", attempts)
    async with factory() as fresh:
        remaining = await fresh.scalar(select(FileResource.id).where(FileResource.id == resource_id))
        links = await fresh.scalar(linked_count)
    assert (deleted, remaining, links) == (0, resource_id, 1)
