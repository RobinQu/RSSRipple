"""API tests for queue monitoring endpoints (read-only live snapshots)."""

import asyncio

import pytest
import pytest_asyncio

from app.services.task_queue import JobStatus, MemoryQueue


async def _wait_status(queue: MemoryQueue, key: str, status: str, timeout: float = 2.0) -> dict:
    """Poll queue.status(key) until the job reaches the expected state."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        state = await queue.status(key)
        if state and state["status"] == status:
            return state
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Job {key!r} never reached {status}; last state={state}")
        await asyncio.sleep(0.02)


@pytest_asyncio.fixture
async def seeded_queue(client, monkeypatch):
    """Point the API at a real MemoryQueue with one job per status."""
    from app.services import task_queue as tq_mod

    queue = MemoryQueue()  # max_concurrent=1 keeps the 4th job queued
    gate = asyncio.Event()

    async def ok_handler(payload):
        return {"ok": True}

    async def bad_handler(payload):
        raise ValueError("boom")

    async def slow_handler(payload):
        await gate.wait()

    queue.register("alpha", ok_handler)
    queue.register("beta", bad_handler)
    queue.register("gamma", slow_handler)
    await queue.start()

    await queue.enqueue("alpha", "job:done", {})
    await _wait_status(queue, "job:done", JobStatus.DONE)
    await queue.enqueue("beta", "job:failed", {})
    await _wait_status(queue, "job:failed", JobStatus.FAILED)
    await queue.enqueue("gamma", "job:running", {})
    await _wait_status(queue, "job:running", JobStatus.RUNNING)
    # The single execution slot is held by the running job, so this stays queued.
    await queue.enqueue("alpha", "job:queued", {})

    monkeypatch.setattr(tq_mod, "task_queue", queue)
    yield client
    gate.set()
    await queue.stop()


@pytest.mark.asyncio
async def test_queue_overview_aggregation(seeded_queue):
    res = await seeded_queue.get("/api/v1/queue/overview")
    assert res.status_code == 200
    data = res.json()["data"]

    assert data["backend"] == "memory"
    assert data["app_role"] == "all"
    assert data["stats_scope"] == "since_restart"
    assert data["total"] == 4
    assert data["counts"] == {"queued": 1, "running": 1, "done": 1, "failed": 1}

    by_type = {entry["job_type"]: entry for entry in data["by_type"]}
    alpha = by_type["alpha"]
    assert alpha["total"] == 2
    assert alpha["done"] == 1
    assert alpha["queued"] == 1
    assert alpha["success_rate"] == 1.0
    assert alpha["avg_duration_seconds"] is not None

    beta = by_type["beta"]
    assert beta["total"] == 1
    assert beta["failed"] == 1
    assert beta["success_rate"] == 0.0

    gamma = by_type["gamma"]
    assert gamma["total"] == 1
    assert gamma["running"] == 1
    assert gamma["success_rate"] is None
    assert gamma["avg_duration_seconds"] is None


@pytest.mark.asyncio
async def test_queue_overview_empty(client, monkeypatch):
    from app.services import task_queue as tq_mod

    monkeypatch.setattr(tq_mod, "task_queue", MemoryQueue())
    res = await client.get("/api/v1/queue/overview")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["total"] == 0
    assert data["counts"] == {"queued": 0, "running": 0, "done": 0, "failed": 0}
    assert data["by_type"] == []


@pytest.mark.asyncio
async def test_queue_jobs_order_and_pagination(seeded_queue):
    res = await seeded_queue.get("/api/v1/queue/jobs")
    assert res.status_code == 200
    body = res.json()
    assert body["meta"] == {"page": 1, "page_size": 50, "total": 4}
    statuses = [job["status"] for job in body["data"]]
    # running first, then queued, then terminal by queued_at descending.
    assert statuses[:2] == ["running", "queued"]
    assert set(statuses[2:]) == {"done", "failed"}

    res = await seeded_queue.get("/api/v1/queue/jobs", params={"page": 2, "page_size": 2})
    body = res.json()
    assert body["meta"] == {"page": 2, "page_size": 2, "total": 4}
    assert len(body["data"]) == 2

    res = await seeded_queue.get("/api/v1/queue/jobs", params={"page_size": 101})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_queue_jobs_filters(seeded_queue):
    res = await seeded_queue.get("/api/v1/queue/jobs", params={"status": "running"})
    assert [job["key"] for job in res.json()["data"]] == ["job:running"]
    assert res.json()["meta"]["total"] == 1

    res = await seeded_queue.get("/api/v1/queue/jobs", params={"job_type": "alpha"})
    body = res.json()
    assert body["meta"]["total"] == 2
    # queued ranks ahead of terminal history.
    assert [job["status"] for job in body["data"]] == ["queued", "done"]

    res = await seeded_queue.get(
        "/api/v1/queue/jobs", params={"status": "done", "job_type": "beta"}
    )
    assert res.json()["meta"]["total"] == 0
    assert res.json()["data"] == []


@pytest.mark.asyncio
async def test_queue_scheduler_uninitialized(client, monkeypatch):
    import app.services.scheduler as sch_mod

    monkeypatch.setattr(sch_mod, "_scheduler", None)
    res = await client.get("/api/v1/queue/scheduler")
    assert res.status_code == 200
    assert res.json()["data"] == {"enabled": False, "jobs": []}


@pytest.mark.asyncio
async def test_queue_scheduler_snapshot(client, monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    import app.services.scheduler as sch_mod

    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: None, trigger=IntervalTrigger(minutes=1), id="sync_progress")
    monkeypatch.setattr(sch_mod, "_scheduler", scheduler)

    res = await client.get("/api/v1/queue/scheduler")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["enabled"] is True
    assert data["jobs"][0]["id"] == "sync_progress"
    assert "interval" in data["jobs"][0]["trigger"]
