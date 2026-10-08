"""Re-run the recorded-resource orphan probe against the lease candidate.

Real child process exit and database-clock expiry; no Redis worker takeover.
The original setup/fault boundary is reused without weakening its data inputs.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


async def child(action):
    from agent_run_orphan_probe import phase
    from sqlalchemy import select

    from app import database
    from app.models.agent_run import AgentRun
    from app.models.agent_run_lease import AgentRunLease
    from app.services import agent_run_execution
    from app.services.agent_run_lifecycle import reap_expired_runs

    agent_run_execution.LEASE_SECONDS = 3
    agent_run_execution.HEARTBEAT_SECONDS = 0.25
    try:
        if action == "reap":
            return await reap_expired_runs()
        if action != "inspect":
            return await phase(action)
        rows = await phase("inspect")
        agent_id = json.loads(os.environ["PROBE_IDS"])["agent"]
        async with database.async_session_factory() as db:
            leases = list(await db.scalars(select(AgentRunLease.id).where(
                AgentRunLease.run_id.in_(select(AgentRun.id).where(AgentRun.agent_id == agent_id)),
            )))
        return {"runs": rows, "lease_count": len(leases)}
    finally:
        await database.engine.dispose()


def call(action, env, log, expected=0):
    result = subprocess.run([sys.executable, __file__, "--phase", action],
                            env=env, capture_output=True, text=True, timeout=60)
    log.append({"phase": action, "exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
    assert result.returncode == expected, log[-1]
    return json.loads(result.stdout) if expected == 0 else None


async def main():
    import asyncpg

    reports, log = [], []
    postgres = os.environ["AGENT_RUN_TEST_POSTGRES_URL"]
    parts = urlsplit(postgres)
    assert parts.hostname == "127.0.0.1" and parts.path == "/work_fk_probe"
    name = "agent_run_recovery_" + uuid.uuid4().hex
    admin = await asyncpg.connect(urlunsplit(parts._replace(scheme="postgresql")))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        with tempfile.TemporaryDirectory(prefix="rssripple-v34-recovery-") as temporary:
            urls = [
                ("turso", f"sqlite+aioturso:///{temporary}/probe.db"),
                ("postgresql", urlunsplit(parts._replace(scheme="postgresql+asyncpg", path="/" + name))),
            ]
            for backend, url in urls:
                for failure in ["exception", "exit"]:
                    ids = {key: str(uuid.uuid4()) for key in ["channel", "downloader", "agent", "resource"]}
                    env = dict(os.environ, DATABASE_URL=url, PROBE_IDS=json.dumps(ids), PROBE_FAILURE=failure)
                    call("setup", env, log)
                    call("fail", env, log, 73 if failure == "exit" else 1)
                    before = call("inspect", env, log)
                    assert len(before["runs"]) == 1
                    assert before["runs"][0]["status"] == ("running" if failure == "exit" else "failed")
                    assert before["lease_count"] == (1 if failure == "exit" else 0)
                    deadline = time.monotonic() + 10
                    reaped = []
                    while failure == "exit" and not reaped:
                        reaped = call("reap", env, log)
                        assert time.monotonic() < deadline, "Natural lease expiry did not become reclaimable"
                        if not reaped:
                            await asyncio.sleep(0.2)
                    if failure == "exit":
                        assert reaped == [before["runs"][0]["id"]]
                    recovered = call("recover", env, log)
                    assert recovered["total_resources"] == recovered["unrecognized"] == 1
                    assert recovered["dispatched"] == 0
                    after = call("inspect", env, log)
                    assert sorted(row["status"] for row in after["runs"]) == ["failed", "success"]
                    assert all(row["finished"] for row in after["runs"]) and after["lease_count"] == 0
                    reports.append({"backend": backend, "failure": failure, "before": before,
                                    "reaped": reaped, "after": after})
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
        Path(os.environ["PROBE_RESULT"]).write_text(json.dumps({
            "cases": reports, "logs": log, "database_cleanup": True,
            "scope": "Actual handler/child exit/DB-clock expiry; no Redis or scheduler takeover",
        }, ensure_ascii=False, indent=2) + "\n")
    assert len(reports) == 4
    print(json.dumps({"passed_cases": len(reports)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase")
    args = parser.parse_args()
    if args.phase:
        print(json.dumps(asyncio.run(child(args.phase)), ensure_ascii=False))
    else:
        asyncio.run(main())
