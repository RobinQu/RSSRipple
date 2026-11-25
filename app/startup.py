"""Shared process startup wiring for the web and worker entry points.

``app.main`` serves HTTP while ``app.worker`` consumes the queue, but both
must create the same data directories, run the same DDL/runtime-config
bootstrap and start the same scheduler/queue machinery. Keeping that sequence
here prevents the two entry points from drifting apart.
"""

import logging
from pathlib import Path

from app.config import settings
from app.database import async_session_factory, create_tables

logger = logging.getLogger(__name__)


def configure_logging() -> None:  # pragma: no cover - process wiring
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("app").setLevel(settings.log_level)


def ensure_data_dirs() -> None:  # pragma: no cover - process wiring
    """Create the poster cache dir and the SQLite DB parent dir.

    Both roles download posters during metadata jobs and open the same
    database file, so the rules (and the fallback) must stay identical.
    """
    poster_dir = Path(settings.poster_cache_dir)
    try:
        poster_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Fallback to local data/posters if configured path is unwritable
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


async def initialize_database() -> None:  # pragma: no cover - process wiring
    if settings.db_migrate_on_startup:
        logger.info("Creating database tables...")
        await create_tables()
        logger.info("Database ready.")
    else:
        # Distributed compose stack: DDL ran in the one-shot `migrate`
        # service this stack depends on — skip it here.
        logger.info("Skipping startup DDL (DB_MIGRATE_ON_STARTUP=false).")


async def load_runtime_settings() -> None:  # pragma: no cover - process wiring
    """Load runtime-configurable settings from the DB into the memory cache."""
    from app.services.publication_startup import ensure_publication_ready
    from app.services.runtime_config import load_runtime_config

    async with async_session_factory() as publication_db:
        await ensure_publication_ready(publication_db)
        await publication_db.commit()

    async with async_session_factory() as sess:
        await load_runtime_config(sess)
    logger.info("Runtime settings loaded.")


async def bootstrap_auth() -> None:  # pragma: no cover - process wiring
    """Persist credentials without publishing them to application logs.

    Enrollment is an explicit operator action via the local CLI.
    """
    from app.services.auth_service import (
        get_or_create_cookie_secret,
        get_or_create_totp_secret,
    )

    async with async_session_factory() as sess:
        await get_or_create_totp_secret(sess)
        await get_or_create_cookie_secret(sess)
        await sess.commit()
    logger.info("Authentication ready. See deployment docs to enroll an authenticator.")


async def start_scheduler() -> None:  # pragma: no cover - process wiring
    from app.services.scheduler import init_scheduler, setup_channel_jobs

    await init_scheduler()
    async with async_session_factory() as sess:
        await setup_channel_jobs(sess)
        await sess.commit()


async def start_task_queue(consume: bool):  # pragma: no cover - process wiring
    """Build the queue, register every handler and start consuming."""
    import app.services.task_queue as task_queue_module
    from app.job_handlers import register_all_handlers
    from app.services.task_queue import create_queue

    queue = create_queue(
        backend=settings.queue_backend,
        redis_url=settings.redis_url,
        max_concurrent=settings.queue_max_concurrent,
    )
    task_queue_module.task_queue = queue
    register_all_handlers(queue)
    await queue.start(consume=consume)
    return queue
