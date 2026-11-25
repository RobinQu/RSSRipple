"""Worker process entry point (``python -m app.worker``).

Runs the scheduler and consumes the task queue; serves no HTTP. Use together
with one or more ``APP_ROLE=web`` web processes in a split deployment (the
default ``APP_ROLE=all`` standalone process needs no separate worker).

Startup mirrors the web lifespan minus anything HTTP-specific (no FastAPI
app, no routers/static mounts, no auth bootstrap).
"""

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

# Import models for SQLAlchemy discovery
import app.models  # noqa: F401
from app.config import settings
from app.startup import (
    configure_logging,
    ensure_data_dirs,
    initialize_database,
    load_runtime_settings,
    start_scheduler,
    start_task_queue,
)

logger = logging.getLogger("app.worker")

# The worker serves no HTTP, so the container healthcheck cannot probe a
# health endpoint. Instead the worker touches this file periodically and the
# healthcheck asserts it is fresh — a wedged event loop stops refreshing it
# and flips the container unhealthy.
HEARTBEAT_PATH = Path("/tmp/rssripple_worker_heartbeat")
HEARTBEAT_INTERVAL = 30


async def _run() -> None:  # pragma: no cover - process wiring
    configure_logging()

    if settings.app_role == "web":
        logger.warning("APP_ROLE=web but the worker entry point was started; running as a worker anyway")

    ensure_data_dirs()
    await initialize_database()
    await load_runtime_settings()
    await start_scheduler()
    queue = await start_task_queue(consume=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    heartbeat_task = asyncio.create_task(_heartbeat_loop(stop_event))

    logger.info("Worker started (queue_backend=%s)", settings.queue_backend)
    try:
        await stop_event.wait()
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        from app.services.scheduler import shutdown_scheduler

        await queue.stop()
        await shutdown_scheduler()
        logger.info("Worker shut down.")


async def _heartbeat_loop(stop_event: asyncio.Event) -> None:
    """Touch the heartbeat file every HEARTBEAT_INTERVAL seconds so the
    container healthcheck can distinguish a live worker from a wedged one."""
    while not stop_event.is_set():
        try:
            HEARTBEAT_PATH.touch()
        except OSError as e:
            logger.warning("Cannot write worker heartbeat %s (%s)", HEARTBEAT_PATH, e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL)
        except TimeoutError:
            continue


def main() -> None:  # pragma: no cover - process wiring
    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
