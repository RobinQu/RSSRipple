"""Reviewed manifest positive control plus explicit malformed RPC injection."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.download_notification import DownloadNotification
from app.models.file_resource import FileResource
from app.models.notification_build_failure import NotificationBuildFailure
from app.models.organize_plan import OrganizePlan
from app.models.series import TVSeries
from app.models.webhook_delivery import WebhookDelivery
from app.models.work_collection import WorkCollection
from app.services.notify_service import create_notification_for_task, ensure_deliveries
from app.services.scheduler import _process_download_notifications
from app.services.torrent_inspect import parse_torrent_payload
from tests.integration.organize.test_organize_pipeline import TV_TEMPLATE, _library, _rule, _seed_chain
from tests.metadata_corpus.dataset import ROOT, asset, digest, load_corpus, read_json


@pytest.mark.parametrize("fault", ["malformed_rpc", "after_snapshot_flush"])
async def test_poison_task_does_not_block_existing_delivery_and_plan(
    db_session, session_factory, shared_volume, rpc_mocks, record_testsuite_property, monkeypatch, fault,
):
    manifest, corpus, reviews = load_corpus(ROOT)
    case_id = "f79ef2eb-02d5-42d3-80dc-70dd3c1d733b"
    case = next(row for row in corpus["cases"] if row["id"] == case_id)
    assert reviews[case_id]["status"] == "confirmed"
    [assignment] = reviews[case_id]["expected"]["assignments"]
    assert assignment["season"] == 1 and assignment["episode_start"] == 10
    torrent = asset(ROOT, case["evidence"]["torrent"])
    assert digest(torrent.read_bytes()) == torrent.stem
    listing_file = asset(ROOT, manifest["file_list_file"])
    assert digest(listing_file.read_bytes()) == manifest["file_list_sha256"]
    listing = read_json(listing_file)[case["evidence"]["torrent"]]
    assert parse_torrent_payload(torrent.read_bytes()) == listing
    assert listing[0]["name"] == assignment["file_path"]
    record_testsuite_property("torrent_sha256", torrent.stem)
    record_testsuite_property("real_case", case_id)
    record_testsuite_property("injection", f"{fault}; media bytes synthetic")
    collection = WorkCollection(id=str(uuid.uuid4()), title_cn="猫与龙")
    db_session.add(collection)
    series = TVSeries(id=str(uuid.uuid4()), title_cn="猫与龙", season_number=1,
                      collection_id=collection.id, number_of_episodes=12, content_type="tv")
    async def chain():
        return await _seed_chain(
            db_session, work=series,
            resource_kw=dict(title_raw=case["input"]["title_raw"], season=1, episode=10, is_batch=False),
            download_dir=shared_volume.complete_daemon, volume=shared_volume.volume,
            downloader_dir=shared_volume.daemon,
        )
    good = await chain()
    folder = shared_volume.complete_process / "captured-torrent"
    folder.mkdir()
    [entry] = listing
    sentinel = folder / entry["name"]
    body = b"explicit synthetic media"
    sentinel.write_bytes(body)
    rpc_mocks.get_files.return_value = {"name": folder.name, "files": listing}
    existing, created = await create_notification_for_task(db_session, good.task)
    assert created
    await db_session.commit()
    await ensure_deliveries(db_session)
    bad = await chain()
    bad.task.transmission_torrent_id = 43
    await db_session.commit()
    lib = await _library(db_session, shared_volume.media / "tv")
    await _rule(db_session, lib.id, TV_TEMPLATE)
    rpc_mocks.get_files.side_effect = lambda torrent_id: (
        {"name": folder.name, "files": [None]} if torrent_id == 43
        else {"name": folder.name, "files": listing}
    )
    from app.services import notification_build

    original_create = notification_build.create_notification_for_task
    if fault == "after_snapshot_flush":
        rpc_mocks.get_files.side_effect = None

        async def fail_after_real_flush(db, task):
            result = await original_create(db, task)
            if task.id == bad.task.id:
                resource = await db.get(FileResource, task.file_resource_id)
                resource.title_raw = "must be rolled back"
                await db.flush()
                raise RuntimeError("injected after snapshot flush")
            return result

        monkeypatch.setattr(notification_build, "create_notification_for_task", fail_after_real_flush)
    await _process_download_notifications()
    async with session_factory() as session:
        delivery = (await session.execute(select(WebhookDelivery).where(
            WebhookDelivery.notification_id == existing.id,
        ))).scalar_one()
        assert delivery.status == "done", "one malformed task blocked an existing delivery"
        plan = (await session.execute(select(OrganizePlan).where(
            OrganizePlan.notification_id == existing.id,
        ))).scalar_one()
        assert plan.status == "pending"
        assert await session.scalar(select(DownloadNotification.id).where(
            DownloadNotification.download_task_id == bad.task.id,
        )) is None
    assert sentinel.read_bytes() == body
    rpc_mocks.remove.assert_not_awaited()

    async with session_factory() as session:
        failure = (await session.execute(select(NotificationBuildFailure))).scalar_one()
        assert failure.download_task_id == bad.task.id and failure.attempt_count == 1
        assert ("NoneType" if fault == "malformed_rpc" else "injected") in failure.error_message
        resource = await session.get(FileResource, bad.resource.id)
        assert resource.title_raw == case["input"]["title_raw"]
        due = failure.next_attempt_at
    assert sum(call.args == (43,) for call in rpc_mocks.get_files.await_args_list) == 1
    fresh = await chain()
    fresh.task.transmission_torrent_id = 44
    await db_session.commit()
    await _process_download_notifications()
    assert sum(call.args == (43,) for call in rpc_mocks.get_files.await_args_list) == 1
    async with session_factory() as session:
        fresh_notification = await session.scalar(select(DownloadNotification).where(
            DownloadNotification.download_task_id == fresh.task.id,
        ))
        assert fresh_notification is not None
        assert await session.scalar(select(OrganizePlan.id).where(
            OrganizePlan.notification_id == fresh_notification.id,
        ))
    # Corrected RPC data is retried after persisted backoff, without restarting.
    monkeypatch.setattr("app.services.scheduler.utcnow", lambda: due + timedelta(seconds=1))
    monkeypatch.setattr("app.services.notification_build.utcnow", lambda: due + timedelta(seconds=1))
    rpc_mocks.get_files.side_effect = None
    monkeypatch.setattr(notification_build, "create_notification_for_task", original_create)
    rpc_mocks.get_files.return_value = {"name": folder.name, "files": listing}
    await _process_download_notifications()
    async with session_factory() as session:
        assert await session.scalar(select(NotificationBuildFailure.id)) is None
        assert await session.scalar(select(DownloadNotification.id).where(
            DownloadNotification.download_task_id == bad.task.id,
        ))
    assert sentinel.read_bytes() == body
    rpc_mocks.remove.assert_not_awaited()
