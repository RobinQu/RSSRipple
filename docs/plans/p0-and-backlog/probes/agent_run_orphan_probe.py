"""Actual handler commits and child process loss; no live queue or downloader."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


async def phase(name):
    from sqlalchemy import select, text

    import app.models  # noqa: F401
    from app import database
    from app.job_handlers import _handle_run_agent
    from app.models.agent import Agent
    from app.models.agent_run import AgentRun
    from app.models.channel import Channel
    from app.models.downloader import DownloaderInstance
    from app.models.file_resource import FileResource

    ids = json.loads(os.environ["PROBE_IDS"])
    try:
        if name == "setup":
            raw = Path("tests/fixtures/prod_works_v1.json").read_bytes()
            assert hashlib.sha256(raw).hexdigest() == "d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32"
            recorded = json.loads(raw)["tables"]["file_resources"][0]
            async with database.engine.begin() as conn:
                await conn.run_sync(database.Base.metadata.create_all)
                if conn.dialect.name == "sqlite":
                    await conn.execute(text("PRAGMA journal_mode='mvcc'"))
            async with database.async_session_factory() as db:
                db.add(
                    Channel(
                        id=ids["channel"],
                        name="Synthetic orphan history",
                        type="rss_feed",
                        url="https://synthetic.invalid/no-network",
                        field_mapping={},
                    )
                )
                db.add(
                    DownloaderInstance(
                        id=ids["downloader"],
                        name="Synthetic",
                        type="mock",
                        url="mock://synthetic",
                        download_dir="/tmp/no-media",
                    )
                )
                await db.flush()
                db.add(
                    Agent(
                        id=ids["agent"],
                        name="Synthetic",
                        channel_id=ids["channel"],
                        downloader_id=ids["downloader"],
                        scope_channel_wide=True,
                        llm_enabled=False,
                    )
                )
                db.add(
                    FileResource(
                        id=ids["resource"],
                        channel_id=ids["channel"],
                        guid=recorded["guid"],
                        title_raw=recorded["title_raw"],
                        torrent_url=recorded["torrent_url"],
                    )
                )
                await db.commit()
            return {"setup": True}
        if name == "inspect":
            async with database.async_session_factory() as db:
                rows = list(
                    await db.scalars(
                        select(AgentRun)
                        .where(AgentRun.agent_id == ids["agent"])
                        .order_by(AgentRun.started_at, AgentRun.id)
                    )
                )
                return [
                    {
                        "id": row.id,
                        "status": row.status,
                        "finished": row.finished_at is not None,
                        "total_resources": row.total_resources,
                        "dispatched": row.dispatched,
                        "unrecognized": row.unrecognized,
                        "errors": row.errors,
                    }
                    for row in rows
                ]
        if name == "fail":
            from app.services import agent_service

            async def injected_failure(*args, **kwargs):
                # Entered only after the actual phase-one transaction committed.
                if os.environ["PROBE_FAILURE"] == "exit":
                    os._exit(73)
                raise RuntimeError("synthetic unexpected processing failure")

            agent_service.process_resources = injected_failure
        return await _handle_run_agent({"agent_id": ids["agent"], "resource_ids": [ids["resource"]]})
    finally:
        await database.engine.dispose()


def run_child(action, env, logs):
    result = subprocess.run(
        [sys.executable, __file__, "--phase", action], env=env, capture_output=True, text=True, timeout=60
    )
    logs.append({"phase": action, "exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
    return result


def run_probe():
    reports = []
    logs = []
    with tempfile.TemporaryDirectory(prefix="rssripple-v34-orphan-") as temporary:
        urls = [("turso", f"sqlite+aioturso:///{temporary}/probe.db")]
        if os.environ.get("AGENT_RUN_TEST_POSTGRES_URL"):
            urls.append(("postgresql", os.environ["AGENT_RUN_TEST_POSTGRES_URL"]))
        for backend, url in urls:
            for failure in ["exception", "exit"]:
                ids = {name: str(uuid.uuid4()) for name in ["channel", "downloader", "agent", "resource"]}
                env = dict(os.environ, DATABASE_URL=url, PROBE_IDS=json.dumps(ids), PROBE_FAILURE=failure)
                setup = run_child("setup", env, logs)
                assert setup.returncode == 0, setup.stderr
                failed = run_child("fail", env, logs)
                assert failed.returncode == (73 if failure == "exit" else 1), failed.stderr
                before = run_child("inspect", env, logs)
                assert before.returncode == 0, before.stderr
                before_rows = json.loads(before.stdout)
                assert len(before_rows) == 1 and before_rows[0]["status"] == "running"
                recovered = run_child("recover", env, logs)
                assert recovered.returncode == 0, recovered.stderr
                after = run_child("inspect", env, logs)
                assert after.returncode == 0, after.stderr
                after_rows = json.loads(after.stdout)
                assert len(after_rows) == 2
                success = [r for r in after_rows if r["status"] == "success"]
                assert len(success) == 1 and success[0]["total_resources"] == 1
                assert success[0]["unrecognized"] == 1 and success[0]["dispatched"] == 0
                reports.append(
                    {
                        "backend": backend,
                        "failure": failure,
                        "before": before_rows,
                        "after": after_rows,
                        "orphan_remains": any(r["status"] == "running" for r in after_rows),
                    }
                )
    output = {
        "cases": reports,
        "logs": logs,
        "scope": (
            "Real handler and DB commits; process_resources fault boundary; "
            "no broker/worker lease recovery; recorded title/GUID/URL "
            "with synthetic identity and unlinked resource"
        ),
    }
    Path(os.environ["PROBE_RESULT"]).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"cases": len(reports), "orphan_cases": sum(r["orphan_remains"] for r in reports)}))
    assert not any(r["orphan_remains"] for r in reports), (
        "Terminal attempts must not remain running after failure/recovery"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["setup", "fail", "inspect", "recover"])
    args = parser.parse_args()
    if args.phase:
        print(json.dumps(asyncio.run(phase(args.phase))))
    else:
        run_probe()
