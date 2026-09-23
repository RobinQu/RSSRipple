"""Persistent dispatch identity: frozen parameters, result reuse and deletion."""

import uuid

import pytest
from sqlalchemy import func, select

from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.services.download_dispatch import operation_key, persist_dispatch_result, reserve_dispatch


async def test_reservation_reuses_identity_and_rejects_parameter_drift(db_session):
    key = operation_key(("agent:x", "job"), "resource", "agent")
    parameters = {"download_dir": "/downloads", "payload_digest": "captured"}
    first = await reserve_dispatch(db_session.bind, key, parameters)
    second = await reserve_dispatch(db_session.bind, key, parameters)
    assert first.task_id == second.task_id
    assert uuid.UUID(first.task_id).version == 4
    with pytest.raises(ValueError, match="parameters changed"):
        await reserve_dispatch(db_session.bind, key, {**parameters, "download_dir": "/elsewhere"})
    other = await reserve_dispatch(db_session.bind, operation_key(("agent:x", "new-job"), "resource", "agent"), parameters)
    assert other.task_id != first.task_id
    assert operation_key(("agent:x", "job"), "other-resource", "agent") != key
    assert operation_key(("agent:x", "job"), "resource", "other-agent") != key


async def test_result_reuses_task_and_does_not_recreate_deleted_task(db_session):
    channel = Channel(name="synthetic", type="rss_feed", url="https://example.invalid", field_mapping={})
    downloader = DownloaderInstance(name="synthetic", type="mock", url="http://example.invalid", download_dir="/downloads")
    db_session.add_all([channel, downloader])
    await db_session.flush()
    resource = FileResource(channel_id=channel.id, guid=str(uuid.uuid4()), title_raw="Synthetic", torrent_url="magnet:?xt=synthetic")
    db_session.add(resource)
    await db_session.commit()
    values = {"file_resource_id": resource.id, "downloader_id": downloader.id, "download_dir": "/downloads", "status": "downloading", "transmission_torrent_id": 1}
    reservation = await reserve_dispatch(db_session.bind, "a" * 64, {"fixture": "synthetic"})
    task = await persist_dispatch_result(db_session, reservation, values)
    await db_session.commit()
    replay = await persist_dispatch_result(db_session, reservation, {**values, "status": "error"})
    await db_session.commit()
    assert replay.id == task.id and replay.status == "downloading"
    assert await db_session.scalar(select(func.count()).select_from(DownloadTask)) == 1
    await db_session.delete(task)
    await db_session.commit()
    with pytest.raises(ValueError, match="removed"):
        await persist_dispatch_result(db_session, reservation, values)
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(DownloadTask)) == 0


async def test_raw_insert_reservation_defaults_to_unsettled(db_session):
    from sqlalchemy import text

    from app.models.download_dispatch import DownloadDispatch

    identifier = str(uuid.uuid4())
    await db_session.execute(text(
        "INSERT INTO download_dispatches (id, operation_key, task_id, parameters) "
        "VALUES (:id, :operation, :task, :parameters)"
    ), {"id": identifier, "operation": "b" * 64, "task": str(uuid.uuid4()), "parameters": "{}"})
    assert await db_session.scalar(select(DownloadDispatch.settled).where(DownloadDispatch.id == identifier)) is False


async def test_result_rollback_preserves_reservation_for_retry(db_session):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.models.download_dispatch import DownloadDispatch

    channel = Channel(name="rollback", type="rss_feed", url="https://example.invalid", field_mapping={})
    downloader = DownloaderInstance(name="rollback", type="mock", url="http://example.invalid", download_dir="/downloads")
    db_session.add_all([channel, downloader])
    await db_session.flush()
    resource = FileResource(channel_id=channel.id, guid=str(uuid.uuid4()), title_raw="Synthetic", torrent_url="magnet:?xt=synthetic")
    db_session.add(resource)
    await db_session.commit()
    values = {"file_resource_id": resource.id, "downloader_id": downloader.id, "download_dir": "/downloads", "status": "downloading", "transmission_torrent_id": 1}
    key, parameters = "c" * 64, {"fixture": "rollback"}
    reservation = await reserve_dispatch(db_session.bind, key, parameters)
    await persist_dispatch_result(db_session, reservation, values)
    with pytest.raises(IntegrityError):
        await db_session.execute(text("UPDATE download_tasks SET download_dir = NULL WHERE id = :id"), {"id": reservation.task_id})
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(DownloadTask)) == 0
    assert await db_session.scalar(select(DownloadDispatch.settled).where(DownloadDispatch.operation_key == key)) is False
    await db_session.rollback()
    replay = await reserve_dispatch(db_session.bind, key, parameters)
    assert replay.task_id == reservation.task_id
    task = await persist_dispatch_result(db_session, replay, values)
    await db_session.commit()
    assert task.id == reservation.task_id
    assert await db_session.scalar(select(func.count()).select_from(DownloadTask)) == 1


async def test_connection_bound_caller_cannot_roll_back_reservation(db_session):
    from app.models.download_dispatch import DownloadDispatch

    key = "d" * 64
    async with db_session.bind.connect() as connection:
        outer = await connection.begin()
        reservation = await reserve_dispatch(connection, key, {"fixture": "external-connection"})
        await outer.rollback()
    assert await db_session.scalar(select(DownloadDispatch.task_id).where(DownloadDispatch.operation_key == key)) == reservation.task_id
