"""Queue monitoring API routes (read-only live snapshots, no persistence)."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from app.config import settings
from app.schemas.common import paginated_response, success_response
from app.services import task_queue as task_queue_module
from app.services.scheduler import get_scheduler
from app.services.task_queue import JobStatus

router = APIRouter()

_TERMINAL_STATUSES = (JobStatus.DONE, JobStatus.FAILED)
# Lower rank sorts first in /queue/jobs: active work ahead of terminal history.
_STATUS_SORT_RANK = {JobStatus.RUNNING: 0, JobStatus.QUEUED: 1}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _job_duration_seconds(job: dict) -> float | None:
    started_at = _parse_iso(job.get("started_at"))
    finished_at = _parse_iso(job.get("finished_at"))
    if started_at is None or finished_at is None:
        return None
    if (started_at.tzinfo is None) != (finished_at.tzinfo is None):
        return None  # naive/aware mixes cannot be subtracted
    return (finished_at - started_at).total_seconds()


def _sort_jobs(jobs: list[dict]) -> list[dict]:
    # Stable two-pass: queued_at descending within each status group (jobs
    # missing queued_at — e.g. cleared Redis hashes — sort last), then rank.
    by_time = sorted(jobs, key=lambda j: j.get("queued_at") or "", reverse=True)
    return sorted(
        by_time, key=lambda j: _STATUS_SORT_RANK.get(j.get("status"), 2)
    )


def _aggregate_by_type(jobs: list[dict]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for job in jobs:
        job_type = job.get("job_type") or ""
        bucket = buckets.setdefault(job_type, {
            "job_type": job_type,
            "total": 0,
            "queued": 0,
            "running": 0,
            "done": 0,
            "failed": 0,
            "durations": [],
        })
        bucket["total"] += 1
        status = job.get("status")
        if status in ("queued", "running", "done", "failed"):
            bucket[status] += 1
        if status in _TERMINAL_STATUSES:
            duration = _job_duration_seconds(job)
            if duration is not None:
                bucket["durations"].append(duration)

    summaries: list[dict[str, Any]] = []
    for bucket in buckets.values():
        finished = bucket["done"] + bucket["failed"]
        durations = bucket.pop("durations")
        summaries.append({
            **bucket,
            "success_rate": bucket["done"] / finished if finished else None,
            "avg_duration_seconds": (
                sum(durations) / len(durations) if durations else None
            ),
        })
    summaries.sort(key=lambda b: (-b["total"], b["job_type"]))
    return summaries


@router.get("/queue/overview")
async def get_queue_overview():
    """Aggregate live queue job state: per-status counts + per-type stats."""
    jobs = await task_queue_module.task_queue.list_jobs()
    counts = {"queued": 0, "running": 0, "done": 0, "failed": 0}
    for job in jobs:
        status = job.get("status")
        if status in counts:
            counts[status] += 1
    return success_response({
        "backend": settings.queue_backend,
        "app_role": settings.app_role,
        "stats_scope": "last_24h" if settings.queue_backend == "redis" else "since_restart",
        "total": len(jobs),
        "counts": counts,
        "by_type": _aggregate_by_type(jobs),
    })


@router.get("/queue/jobs")
async def list_queue_jobs(
    status: str | None = Query(None),
    job_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """List live queue jobs with in-memory filter/sort/pagination."""
    jobs = await task_queue_module.task_queue.list_jobs()
    if status:
        jobs = [job for job in jobs if job.get("status") == status]
    if job_type:
        jobs = [job for job in jobs if job.get("job_type") == job_type]
    jobs = _sort_jobs(jobs)
    total = len(jobs)
    items = jobs[(page - 1) * page_size:page * page_size]
    return paginated_response(items, total=total, page=page, page_size=page_size)


@router.get("/queue/scheduler")
async def get_queue_scheduler():
    """Snapshot of the APScheduler periodic jobs driving queue ticks."""
    try:
        scheduler = get_scheduler()
    except RuntimeError:
        # Scheduler not initialized (APP_ROLE=web or SCHEDULER_ENABLED=false).
        return success_response({"enabled": False, "jobs": []})
    jobs = []
    for job in scheduler.get_jobs():
        # Pending jobs (scheduler created but not started) have no
        # next_run_time attribute at all on APScheduler 3.x.
        next_run_time = getattr(job, "next_run_time", None)
        jobs.append({
            "id": job.id,
            "trigger": str(job.trigger),
            "next_run_time": next_run_time.isoformat() if next_run_time else None,
        })
    return success_response({"enabled": True, "jobs": jobs})
