"""Expired queue ownership must not start or acknowledge webhook delivery."""

import asyncio

import fakeredis
import pytest

from app.services import notify_service
from app.services.task_queue import RedisQueue
from tests.unit import test_notify_service as notify_tests
from tests.unit.test_notify_service import _deliveries, _FakeResp, _webhook

seed = notify_tests.seed


@pytest.mark.parametrize("phase", ["snapshot", "invalidated"])
async def test_resource_regeneration_loss_rolls_back_snapshot_and_tokens(db_session, seed, monkeypatch, phase):
    from app.database import async_session_factory, committed_session
    from app.models.download_notification import DownloadNotification
    from app.models.webhook_delivery import WebhookDelivery
    from app.services import task_queue

    db_session.add(_webhook(seed.agent.id))
    notification, _ = await notify_service.create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    await notify_service.ensure_deliveries(db_session)
    [delivery] = await _deliveries(db_session, notification.id)
    old_payload = notification.payload
    old_token = delivery.attempt_token
    notification_id, delivery_id = notification.id, delivery.id
    expired = False
    original_invalidate = notify_service._invalidate_delivery_attempts

    async def snapshot(*args):
        nonlocal expired
        expired = phase == "snapshot"
        return {**old_payload, "test_stale_marker": True}, True

    async def invalidate(*args):
        nonlocal expired
        await original_invalidate(*args)
        expired = True

    async def guard():
        if expired:
            raise task_queue.ExecutionOwnershipLostError("regeneration expired")

    from unittest.mock import AsyncMock

    plans = AsyncMock()
    monkeypatch.setattr(notify_service, "_build_snapshot", snapshot)
    monkeypatch.setattr(notify_service, "_invalidate_delivery_attempts", invalidate)
    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    monkeypatch.setattr("app.services.organize_service.plan_for_notifications", plans)
    with pytest.raises(task_queue.ExecutionOwnershipLostError):
        async with committed_session() as working:
            await notify_service.regenerate_resource_notifications(working, seed.resource.id)
    plans.assert_not_awaited()
    async with async_session_factory() as observer:
        saved = await observer.get(DownloadNotification, notification_id)
        token = await observer.get(WebhookDelivery, delivery_id)
        assert saved.payload == old_payload
        assert token.attempt_token == old_token


@pytest.mark.parametrize("phase", ["before_send", "after_send"])
async def test_stale_notification_execution_stops_at_side_effect_boundary(db_session, seed, monkeypatch, phase):
    db_session.add(_webhook(seed.agent.id))
    notification, _ = await notify_service.create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    await notify_service.ensure_deliveries(db_session)
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisQueue(redis_client=client)
    entered, resume, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, errors = [], []

    async def revoke():
        await client.hset("rssripple:job:notify-ownership", "execution_token", "replacement")

    class HttpClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            calls.append(url)
            await revoke()
            return _FakeResp()

    monkeypatch.setattr(notify_service.httpx, "AsyncClient", HttpClient)

    async def handler(payload):
        entered.set()
        await resume.wait()
        try:
            await notify_service.deliver_due_deliveries(db_session)
        except Exception as exc:
            errors.append(exc)
            raise
        finally:
            finished.set()

    queue.register("notification-test", handler)
    await queue.start()
    try:
        await queue.enqueue("notification-test", "notify-ownership", {})
        await asyncio.wait_for(entered.wait(), 2)
        if phase == "before_send":
            await revoke()
        resume.set()
        await asyncio.wait_for(finished.wait(), 3)
        assert len(calls) == (0 if phase == "before_send" else 1)
        [delivery] = await _deliveries(db_session, notification.id)
        assert delivery.status == "pending"
        assert delivery.attempt_count == 0
        assert len(errors) == 1 and "lost ownership" in str(errors[0])
    finally:
        resume.set()
        await queue.stop()


async def test_delivery_waits_for_inflight_siblings_before_propagating_loss(db_session, seed, monkeypatch):
    from app.services import task_queue

    db_session.add_all([_webhook(seed.agent.id), _webhook(seed.agent.id)])
    await notify_service.create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    await notify_service.ensure_deliveries(db_session)
    second_started, release, lost = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def guard():
        if lost.is_set():
            raise task_queue.ExecutionOwnershipLostError("Synthetic lost ownership")

    class HttpClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            calls.append(url)
            if len(calls) == 1:
                await second_started.wait()
                lost.set()
            else:
                second_started.set()
                await release.wait()
            return _FakeResp()

    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    monkeypatch.setattr(notify_service.httpx, "AsyncClient", HttpClient)
    running = asyncio.create_task(notify_service.deliver_due_deliveries(db_session))
    try:
        await asyncio.wait_for(lost.wait(), 2)
        await asyncio.sleep(0.05)
        assert not running.done(), "Delivery returned while a sibling still owns its DB session"
    finally:
        release.set()
        result = await asyncio.gather(running, return_exceptions=True)
    assert isinstance(result[0], task_queue.ExecutionOwnershipLostError)
    assert all(delivery.status == "pending" for delivery in await _deliveries(db_session))


async def test_late_failure_preserves_delivery_already_completed(db_session, seed, monkeypatch):
    """Model a winner committing after the loser's final queue check."""
    from sqlalchemy import update

    from app.models.webhook_delivery import WebhookDelivery
    from app.services import task_queue
    from app.utils.time import utcnow

    db_session.add(_webhook(seed.agent.id))
    notification, _ = await notify_service.create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    await notify_service.ensure_deliveries(db_session)
    [delivery] = await _deliveries(db_session, notification.id)
    delivery_id = delivery.id
    winner_time = utcnow()
    checks = 0

    async def guard():
        nonlocal checks
        checks += 1
        if checks == 2:
            # Bypass the identity map: the losing handler still holds its
            # original pending snapshot when the durable winner becomes done.
            await db_session.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == delivery_id)
                .values(status="done", delivered_at=winner_time, error_message=None)
                .execution_options(synchronize_session=False)
            )
            await db_session.commit()

    class HttpClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            raise RuntimeError("late failure from superseded request")

    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    monkeypatch.setattr(notify_service.httpx, "AsyncClient", HttpClient)
    monkeypatch.setattr(notify_service.settings, "notify_max_attempts", 1)
    await notify_service.deliver_due_deliveries(db_session)
    await db_session.refresh(delivery)
    assert checks == 2
    assert delivery.status == "done"
    assert delivery.attempt_count == 0
    assert delivery.error_message is None
    assert delivery.delivered_at == winner_time


@pytest.mark.parametrize("action", ["retry", "regenerate", "resource"])
async def test_snapshot_or_retry_invalidates_inflight_success(db_session, seed, monkeypatch, action):
    from app.services import organize_service

    db_session.add(_webhook(seed.agent.id))
    notification, _ = await notify_service.create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    await notify_service.ensure_deliveries(db_session)
    [delivery] = await _deliveries(db_session, notification.id)
    tokens = []

    async def no_plans(*args, **kwargs):
        return []

    class HttpClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            tokens.append(delivery.attempt_token)
            if action == "retry":
                # Another attempt finishes first, then the user requests a
                # new delivery while this older HTTP request is in flight.
                delivery.status = "done"
                await db_session.commit()
                assert await notify_service.reset_deliveries_for_retry(
                    db_session, "all", notification_id=notification.id
                ) == 1
            elif action == "regenerate":
                assert (await notify_service.regenerate_notifications(
                    db_session, seed.agent.id, None
                ))["regenerated"] == 1
            else:
                assert (await notify_service.regenerate_resource_notifications(
                    db_session, seed.resource.id
                ))["regenerated"] == 1
            tokens.append(delivery.attempt_token)
            return _FakeResp()

    monkeypatch.setattr(organize_service, "plan_for_notifications", no_plans)
    monkeypatch.setattr(notify_service.httpx, "AsyncClient", HttpClient)
    stats = await notify_service.deliver_due_deliveries(db_session)
    await db_session.refresh(delivery)
    assert tokens[0] and tokens[1] and tokens[0] != tokens[1]
    assert delivery.status == "pending"
    assert delivery.attempt_count == 0
    assert stats == {"delivered": 0, "failed": 0, "skipped": 1}


async def test_two_readers_only_one_claims_same_delivery(db_session, db_engine, seed, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.webhook_delivery import WebhookDelivery
    from app.services import task_queue

    db_session.add(_webhook(seed.agent.id))
    notification, _ = await notify_service.create_notification_for_task(db_session, seed.task)
    await db_session.commit()
    await notify_service.ensure_deliveries(db_session)
    [delivery] = await _deliveries(db_session, notification.id)
    delivery_id = delivery.id
    barrier = asyncio.Barrier(2)
    checked = set()
    requests = []

    async def guard():
        task = asyncio.current_task()
        if task not in checked:
            checked.add(task)
            await asyncio.wait_for(barrier.wait(), 3)

    class HttpClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            requests.append(json)
            return _FakeResp()

    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    monkeypatch.setattr(notify_service.httpx, "AsyncClient", HttpClient)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def run():
        async with factory() as db:
            return await notify_service.deliver_due_deliveries(db)

    results = await asyncio.wait_for(asyncio.gather(run(), run(), return_exceptions=True), 8)
    assert all(isinstance(result, dict) for result in results), results
    assert len(requests) == 1
    assert sum(result["delivered"] for result in results) == 1
    assert sum(result["skipped"] for result in results) == 1
    async with factory() as db:
        current = await db.get(WebhookDelivery, delivery_id)
        assert current.status == "done" and current.attempt_count == 0


@pytest.mark.parametrize("phase", ["before_pause", "after_pause", "after_files"])
async def test_snapshot_loss_stops_rpc_chain_without_recording_build_failure(db_session, seed, monkeypatch, phase):
    from sqlalchemy import select

    from app.clients.mock_downloader import MockDownloaderWrapper
    from app.models.download_notification import DownloadNotification
    from app.models.notification_build_failure import NotificationBuildFailure
    from app.services import notification_build, task_queue

    lost = phase == "before_pause"
    calls = []

    async def guard():
        if lost:
            raise task_queue.ExecutionOwnershipLostError("Snapshot worker lost ownership")

    async def pause(self, torrent_id):
        nonlocal lost
        calls.append("pause")
        lost = phase == "after_pause"
        return True

    async def files(self, torrent_id):
        nonlocal lost
        calls.append("files")
        lost = phase == "after_files"
        return {"name": "fixture", "files": [{"name": "fixture.mkv", "size": 10}]}

    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    monkeypatch.setattr(MockDownloaderWrapper, "pause_torrent", pause)
    monkeypatch.setattr(MockDownloaderWrapper, "get_torrent_files", files)
    with pytest.raises(task_queue.ExecutionOwnershipLostError):
        await notification_build.build_task_notifications([seed.task.id])
    assert calls == {
        "before_pause": [], "after_pause": ["pause"], "after_files": ["pause", "files"],
    }[phase]
    assert (await db_session.scalars(select(DownloadNotification))).all() == []
    assert (await db_session.scalars(select(NotificationBuildFailure))).all() == []
