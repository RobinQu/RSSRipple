"""Isolated PostgreSQL cross-process OTP budget probe; synthetic peers only."""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import delete, select

import app.models  # noqa: F401
from app import database
from app.models.app_setting import AppSetting
from app.models.auth_rate_limit import AuthRateLimitBucket
from app.services import auth_rate_limit as limiter

FIXED_NOW = datetime(2026, 9, 23, 12)


def validate_database():
    url = os.environ["AUTH_LIMIT_DATABASE_URL"]
    assert url == os.environ["DATABASE_URL"]
    assert urlsplit(url).path.startswith("/auth_limit_")
    limiter.utcnow = lambda: FIXED_NOW


async def wait_file(path: Path):
    deadline = time.monotonic() + 30
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"Test barrier not released: {path.name}")
        await asyncio.sleep(0.02)


async def worker(mode: str, index: int, directory: Path):
    (directory / f"ready-{index}").touch()
    await wait_file(directory / "go")
    count = 3 if mode == "peer" else 10
    results = []
    for attempt in range(count):
        peer = "192.0.2.1" if mode == "peer" else f"192.0.{index}.{attempt}"
        results.append(await limiter.reserve_otp_attempt(peer))
    print(json.dumps(results))


async def concurrent_round(mode: str, directory: Path):
    directory.mkdir()
    processes = []
    try:
        for index in range(4):
            processes.append(await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tests.integration.auth.rate_limit_driver",
                mode, str(index), str(directory),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            ))
        await asyncio.gather(*(wait_file(directory / f"ready-{index}") for index in range(4)))
        (directory / "go").touch()
        results = []
        for process in processes:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            assert process.returncode == 0, stderr.decode()
            results.extend(json.loads(stdout))
        return results
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def coordinator():
    output = Path(os.environ["AUTH_LIMIT_RESULT"])
    # Simulate an existing database, then add the new table through create_all.
    async with database.engine.begin() as connection:
        await connection.run_sync(AppSetting.__table__.create)
    async with database.async_session_factory() as db, db.begin():
        db.add(AppSetting(key="test-preserved", value="not-a-secret"))
    async with database.engine.begin() as connection:
        await connection.run_sync(database.Base.metadata.create_all)
        await connection.run_sync(database.Base.metadata.create_all)
    async with database.async_session_factory() as db:
        assert (await db.get(AppSetting, "test-preserved")).value == "not-a-secret"
    peer = await concurrent_round("peer", output.parent / "peer")
    assert peer.count(0) == 5, peer
    assert len(peer) == 12
    async with database.async_session_factory() as db, db.begin():
        rows = (await db.execute(select(AuthRateLimitBucket))).scalars().all()
        assert sorted(row.attempts for row in rows) == [5, 12]
        await db.execute(delete(AuthRateLimitBucket))
    global_budget = await concurrent_round("global", output.parent / "global")
    assert global_budget.count(0) == 30, global_budget
    assert len(global_budget) == 40
    # All reserving processes have exited; a new session still sees the denial.
    await database.engine.dispose()
    assert await limiter.reserve_otp_attempt("192.0.2.250") == 60
    async with database.async_session_factory() as db:
        rows = (await db.execute(select(AuthRateLimitBucket))).scalars().all()
        assert len(rows) == 31
    limiter.utcnow = lambda: FIXED_NOW + timedelta(seconds=60)
    assert await limiter.reserve_otp_attempt("192.0.2.250") == 0
    async with database.async_session_factory() as db:
        rows = (await db.execute(select(AuthRateLimitBucket))).scalars().all()
        assert len(rows) == 2
        assert all(row.attempts == 1 for row in rows)
    output.write_text(json.dumps({"cases": [
        "existing-schema-idempotent-addition", "four-process-peer-budget",
        "four-process-global-budget", "budget-survives-process-exit",
        "expired-budget-recovers-and-cleans-up",
    ]}, indent=2))


async def main():
    validate_database()
    try:
        if len(sys.argv) == 4:
            await worker(sys.argv[1], int(sys.argv[2]), Path(sys.argv[3]))
        else:
            await coordinator()
    finally:
        await database.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
