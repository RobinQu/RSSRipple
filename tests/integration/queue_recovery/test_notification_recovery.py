"""Mandatory in the isolated gate; external services are never inferred."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest


@pytest.mark.parametrize("mode", ["kill", "pause", "metadata", "agent", "magnet", "commit", "responsiveness", "organize", "organize_takeover", "multiwork", "descriptor"])
async def test_queue_and_metadata_concurrency(tmp_path, mode):
    admin_url = os.environ.get("QUEUE_RECOVERY_POSTGRES_URL")
    redis_url = os.environ.get("QUEUE_RECOVERY_REDIS_URL")
    if not admin_url or not redis_url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Queue recovery gate requires dedicated PostgreSQL and Redis")
        pytest.skip("Run the isolated integration gate for real queue recovery")
    name = "queue_recovery_" + uuid.uuid4().hex
    parts = urlsplit(admin_url)
    assert parts.path == "/queue_recovery", "Refuse a non-test admin database"
    admin = await asyncpg.connect(admin_url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        output = tmp_path / "recovery.json"
        db_url = urlunsplit(parts._replace(scheme="postgresql+asyncpg", path="/" + name))
        redis_parts = urlsplit(redis_url)
        child_redis = urlunsplit(redis_parts._replace(path="/0" if mode == "kill" else "/1"))
        env = dict(
            os.environ,
            QUEUE_RECOVERY_DATABASE_URL=db_url,
            QUEUE_RECOVERY_REDIS_URL=child_redis,
            QUEUE_RECOVERY_MODE=mode,
            QUEUE_RECOVERY_RESULT=str(output),
        )

        def run_driver():
            module = {name: name + "_driver" for name in ("metadata", "agent", "magnet", "commit", "responsiveness", "organize", "organize_takeover", "multiwork", "descriptor")}.get(mode, "notification_driver")
            with subprocess.Popen(
                [sys.executable, "-m", "tests.integration.queue_recovery." + module],
                cwd=Path(__file__).resolve().parents[3],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            ) as process:
                try:
                    stdout, stderr = process.communicate(timeout=90)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate()
                    raise AssertionError("Recovery driver timed out\n" + stdout + stderr) from None
                return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

        result = await asyncio.to_thread(run_driver)
        (tmp_path / "worker.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(output.read_text())
        if mode == "descriptor":
            assert report["backend"] == "redis"
            assert report["resumed_descriptor_preserved"]
            assert report["empty_list_removed_automatically"]
            return
        if mode == "organize_takeover":
            assert report["child_exit"] == 0 and report["outcomes"] == ["busy", "done"]
            assert report["old_completion_rejected"] and report["target_bytes_preserved"]
            assert report["distinct_replay_job"]
            return
        if mode == "responsiveness":
            assert report["executions"] == 1 and report["child_exit"] == 0
            assert report["renewals"] >= 2
            return
        if mode in {"metadata", "agent", "magnet", "commit", "organize", "multiwork"}:
            assert report["backend"] == "postgresql"
            assert len(report["cases"]) == {"metadata": 17, "agent": 6, "magnet": 8, "commit": 4, "organize": 3, "multiwork": 4}[mode]
            assert all(case["passed"] for case in report["cases"])
            return
        assert report["first_worker_exit"] == (-9 if mode == "kill" else 0)
        assert report["second_worker_exit"] == 0
        assert report["same_job_id"] and report["distinct_committed_tokens"]
        assert report["http_requests"] == 2
        assert report["final_status"] == "done" and report["attempt_count"] == 0
        if mode == "pause":
            assert "lost ownership" in result.stderr
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
