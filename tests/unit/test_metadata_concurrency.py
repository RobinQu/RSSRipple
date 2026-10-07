"""Metadata admission cancellation must not consume capacity or strand a channel."""
import asyncio

import pytest

from app.services.metadata_concurrency import MetadataConcurrency


async def _waiting(concurrency, channel, entered, started):
    started.set()
    async with concurrency.resource(channel):
        entered.set()


@pytest.mark.parametrize('queued_before_conflict', [False, True])
async def test_channel_wait_does_not_block_other_channel_and_cancel_releases_capacity(queued_before_conflict):
    budget = MetadataConcurrency(2)
    entered, started = asyncio.Event(), asyncio.Event()
    task = None
    try:
        async with budget.resource('a') as retry:
            if queued_before_conflict:
                async with budget.resource('b'):
                    task = asyncio.create_task(_waiting(budget, 'a', entered, started))
                    await started.wait()
                    # The waiter has reached the full semaphore before retry
                    # begins. It must recheck channel priority on admission.
                    await asyncio.tasks.sleep(0)
                    retry()
            else:
                retry()
                task = asyncio.create_task(_waiting(budget, 'a', entered, started))
                await started.wait()
            async with asyncio.timeout(5):
                async with budget.resource('b'):
                    assert not entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with asyncio.timeout(5):
            async with budget.resource('a'):
                async with budget.resource('b'):
                    pass
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_multiple_retry_owners_release_channel_only_after_last_owner():
    budget = MetadataConcurrency(3)
    entered, started = asyncio.Event(), asyncio.Event()
    task = None
    try:
        async with budget.resource('a') as retry_one:
            async with budget.resource('a') as retry_two:
                retry_one()
                retry_one()  # More than one failed attempt is still one owner.
                retry_two()
                task = asyncio.create_task(_waiting(budget, 'a', entered, started))
                await started.wait()
            async with asyncio.timeout(5):
                async with budget.resource('b'):
                    assert not entered.is_set()
        async with asyncio.timeout(5):
            await task
        assert entered.is_set()
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
