"""Bound metadata work while giving a channel's pending retries admission priority."""
import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager


class MetadataConcurrency:
    """One fetch/backfill batch owns this object; no process-global retry state.

    A retrying resource keeps its slot. Other resources from its channel wait
    outside the slot budget, so unrelated channels can still make progress.
    This coordinates local admission only; database conflicts still require
    whole-transaction rollback and the existing bounded retry policy.
    """

    def __init__(self, limit: int):
        self._slots = asyncio.Semaphore(limit)
        self._ready: dict[str, asyncio.Event] = {}
        self._retrying: dict[str, int] = {}

    @asynccontextmanager
    async def resource(self, channel_id: str) -> AsyncIterator[Callable[[], None]]:
        ready = self._ready.get(channel_id)
        if ready is None:
            ready = self._ready[channel_id] = asyncio.Event()
            ready.set()
        while True:
            await ready.wait()
            await self._slots.acquire()
            # A same-channel peer may have entered retry while we waited for
            # capacity. Release the slot before waiting for it to drain.
            if ready.is_set():
                break
            self._slots.release()
        marked = False

        def mark_retry() -> None:
            nonlocal marked
            if not marked:
                marked = True
                self._retrying[channel_id] = self._retrying.get(channel_id, 0) + 1
                ready.clear()

        try:
            yield mark_retry
        finally:
            if marked:
                remaining = self._retrying[channel_id] - 1
                if remaining:
                    self._retrying[channel_id] = remaining
                else:
                    del self._retrying[channel_id]
                    ready.set()
            self._slots.release()
