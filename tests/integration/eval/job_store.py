"""DB-backed job store for the Metadata Eval Tool.

Persists background agent jobs to the ``eval_jobs`` table so that
uncompleted jobs survive server restarts and can be automatically resumed.

All functions use ``async_session_factory`` directly (no FastAPI dependency
injection) because they are called from background tasks and startup logic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select, update

logger = logging.getLogger("rssripple.eval")


# ── Create ───────────────────────────────────────────────────────────────


async def create_job(
    job_id: str,
    titles: list[dict[str, Any]],
    title_ids: list[str],
    max_concurrency: int = 3,
) -> None:
    """Insert a new job record into the DB."""
    from app.database import async_session_factory
    from app.models.eval_job import EvalJob

    async with async_session_factory() as db:
        job = EvalJob(
            id=job_id,
            status="running",
            total=len(titles),
            completed=0,
            titles=titles,
            title_ids=title_ids,
            results={},
            error=None,
            max_concurrency=max_concurrency,
        )
        db.add(job)
        await db.commit()
    logger.info(
        "[eval][job_store] created job_id=%s total=%d max_concurrency=%d title_ids_sample=%s",
        job_id, len(titles), max_concurrency, title_ids[:10],
    )


# ── Read ─────────────────────────────────────────────────────────────────


async def get_job(job_id: str) -> dict[str, Any] | None:
    """Return a job as a dict, or ``None`` if not found."""
    from app.database import async_session_factory
    from app.models.eval_job import EvalJob

    async with async_session_factory() as db:
        job = await db.get(EvalJob, job_id)
        if job is None:
            return None
        return _job_to_dict(job)


async def get_running_jobs() -> list[dict[str, Any]]:
    """Return all jobs with status='running' — used on startup to resume."""
    from app.database import async_session_factory
    from app.models.eval_job import EvalJob

    async with async_session_factory() as db:
        result = await db.execute(
            select(EvalJob).where(EvalJob.status == "running")
        )
        return [_job_to_dict(j) for j in result.scalars().all()]


# ── Update ───────────────────────────────────────────────────────────────


async def update_job_result(
    job_id: str,
    title_id: str,
    result: dict[str, Any],
) -> None:
    """Store a single title's result and increment the completed counter.

    Uses read-modify-write within a transaction.  Safe with a single writer
    (the background task) + WAL mode.
    """
    from app.database import async_session_factory
    from app.models.eval_job import EvalJob

    async with async_session_factory() as db:
        job = await db.get(EvalJob, job_id)
        if job is None:
            logger.warning("update_job_result: job %s not found", job_id)
            return

        # Reassign to trigger SQLAlchemy JSON change detection
        results = dict(job.results or {})
        results[title_id] = result
        completed = len(results)
        job.results = results
        job.completed = completed

        await db.commit()
    logger.info(
        "[eval][job_store] result saved job_id=%s title_id=%s completed=%d",
        job_id, title_id, completed,
    )


async def set_job_status(
    job_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Update the job's status (and optionally error message)."""
    from app.database import async_session_factory
    from app.models.eval_job import EvalJob

    async with async_session_factory() as db:
        values: dict[str, Any] = {"status": status}
        if error is not None:
            values["error"] = error
        await db.execute(
            update(EvalJob).where(EvalJob.id == job_id).values(**values)
        )
        await db.commit()
    logger.info("[eval][job_store] status job_id=%s status=%s error=%s", job_id, status, error)


# ── Resume ───────────────────────────────────────────────────────────────


async def resume_running_jobs() -> int:
    """Resume all 'running' jobs after a server restart.

    For each running job:
    1. Find titles without results yet.
    2. If none remain, mark as completed.
    3. Otherwise, spawn a background task to process the remaining titles.

    Returns the number of jobs resumed.
    """
    # Import here to avoid circular import at module load time
    from tests.integration.eval.api import _run_agent_background

    jobs = await get_running_jobs()
    resumed = 0

    for job in jobs:
        job_id = job["id"]
        titles: list[dict] = job.get("titles") or []
        results: dict = job.get("results") or {}
        max_concurrency = job.get("max_concurrency", 3)

        remaining = [t for t in titles if t["id"] not in results]

        if not remaining:
            # Job finished but was never marked completed (server crashed
            # after the last result but before set_job_status)
            await set_job_status(job_id, "completed")
            logger.info("Job %s: marked completed (all titles had results)", job_id)
            continue

        logger.info(
            "Job %s: resuming %d/%d remaining titles",
            job_id, len(remaining), len(titles),
        )
        asyncio.create_task(
            _run_agent_background(job_id, remaining, max_concurrency)
        )
        resumed += 1

    return resumed


# ── Helpers ──────────────────────────────────────────────────────────────


def _job_to_dict(job) -> dict[str, Any]:
    """Convert an EvalJob ORM instance to a plain dict for API responses."""
    return {
        "id": job.id,
        "status": job.status,
        "total": job.total,
        "completed": job.completed,
        "title_ids": job.title_ids or [],
        "titles": job.titles or [],
        "results": job.results or {},
        "error": job.error,
        "max_concurrency": job.max_concurrency,
    }
