"""Isolated integration entrypoint: real application with test source HTTP routing."""
from apscheduler.triggers.interval import IntervalTrigger

from app.services import scheduler as scheduler_service
from tests.integration.server.source_redirect import install

install()

_original_init_scheduler = scheduler_service.init_scheduler
_TEST_TICK_SECONDS = 15


async def _init_test_scheduler() -> None:
    await _original_init_scheduler()
    if scheduler_service._scheduler is None:
        return
    for job_id in ("sync_progress", "download_notifications"):
        scheduler_service._PERIODIC_THROTTLE_TTL[job_id] = _TEST_TICK_SECONDS - 1
        scheduler_service.get_scheduler().reschedule_job(
            job_id,
            trigger=IntervalTrigger(seconds=_TEST_TICK_SECONDS),
            next_run_time=scheduler_service.utcnow(),
        )


scheduler_service.init_scheduler = _init_test_scheduler

from app.main import app  # noqa: E402,F401
