"""Worker process entry point (``python -m app.worker``).

Runs the scheduler and consumes the task queue; serves no HTTP. Use together
with one or more ``APP_ROLE=web`` web processes in a split deployment (the
default ``APP_ROLE=all`` standalone process needs no separate worker).

Startup mirrors the web lifespan minus anything HTTP-specific (no FastAPI
app, no routers/static mounts, no auth bootstrap).
"""

import asyncio
import logging
import signal
from pathlib import Path

# Import models for SQLAlchemy discovery
import app.models  # noqa: F401
from app.config import settings
from app.database import async_session_factory, create_tables

logger = logging.getLogger("app.worker")

# The worker serves no HTTP, so the container healthcheck cannot probe a
# health endpoint. Instead the worker touches this file periodically and the
# healthcheck asserts it is fresh — a wedged event loop stops refreshing it
# and flips the container unhealthy.
HEARTBEAT_PATH = Path("/tmp/rssripple_worker_heartbeat")
HEARTBEAT_INTERVAL = 30


def _ensure_data_dirs() -> None:
    """Create the poster cache dir and the SQLite DB parent dir (same rules
    as the web lifespan — the worker downloads posters during metadata
    jobs and opens the same database file)."""
    poster_dir = Path(settings.poster_cache_dir)
    try:
        poster_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create poster dir %s (%s); falling back to ./data/posters", poster_dir, e)
        poster_dir = Path("data/posters")
        poster_dir.mkdir(parents=True, exist_ok=True)
        settings.poster_cache_dir = str(poster_dir)

    db_url = settings.database_url
    if db_url.startswith("sqlite"):
        if "sqlite:///" in db_url:
            db_path_str = db_url.split("sqlite:///", 1)[-1]
        else:
            db_path_str = db_url.split("sqlite:", 1)[-1].lstrip("/")
        db_path = Path(db_path_str)
        try:
            if db_path.parent and str(db_path.parent) != "":
                db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Cannot create db dir %s (%s)", db_path.parent, e)


async def _run() -> None:  # pragma: no cover - process wiring
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("app").setLevel(settings.log_level)

    if settings.app_role == "web":
        logger.warning("APP_ROLE=web but the worker entry point was started; running as a worker anyway")

    _ensure_data_dirs()

    logger.info("Creating database tables...")
    await create_tables()
    logger.info("Database ready.")

    # Load runtime-configurable settings (LLM + external search source keys)
    # from the DB into the in-memory cache so user overrides take effect. Job
    # handlers also refresh this map per run (see app/job_handlers.py).
    from app.services.runtime_config import load_runtime_config

    async with async_session_factory() as sess:
        await load_runtime_config(sess)
    logger.info("Runtime settings loaded.")

    from app.services.scheduler import (
        init_scheduler,
        setup_channel_jobs,
        setup_metadata_refresh_job,
        shutdown_scheduler,
    )

    await init_scheduler()
    async with async_session_factory() as sess:
        await setup_channel_jobs(sess)
        await sess.commit()

    # Build task queue and register every handler — this process consumes.
    import app.services.task_queue as _tq_mod
    from app.job_handlers import register_all_handlers
    from app.services.task_queue import create_queue

    queue = create_queue(
        backend=settings.queue_backend,
        redis_url=settings.redis_url,
        max_concurrent=settings.queue_max_concurrent,
    )
    _tq_mod.task_queue = queue
    register_all_handlers(queue)
    await queue.start()

    async with async_session_factory() as sess:
        await setup_metadata_refresh_job(sess)
        await sess.commit()

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
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
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
