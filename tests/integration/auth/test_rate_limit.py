"""Required PostgreSQL counterpart of Turso OTP budget tests."""

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


def _run_driver(command, *, cwd, env):
    # A timed-out coordinator must not leave its HTTP/service children alive.
    with subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


async def test_otp_rate_limit_turso_bursts(tmp_path):
    url = f"sqlite+aioturso:///{tmp_path}/auth_limit_test.db"
    output = tmp_path / "result.json"
    result = await asyncio.to_thread(
        _run_driver,
        [sys.executable, "-m", "tests.integration.auth.turso_rate_limit_driver"],
        cwd=Path(__file__).resolve().parents[3],
        env=dict(os.environ, DATABASE_URL=url, AUTH_LIMIT_DATABASE_URL=url, AUTH_LIMIT_RESULT=str(output)),
    )
    (tmp_path / "driver.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(json.loads(output.read_text())["cases"]) == 10


@pytest.mark.parametrize("driver,expected_cases", [
    ("rate_limit_driver", 5), ("rate_limit_http_driver", 4),
], ids=["service", "http"])
async def test_otp_rate_limit_postgres(tmp_path, driver, expected_cases):
    url = os.environ.get("QUEUE_RECOVERY_POSTGRES_URL")
    if not url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("OTP budget gate requires dedicated PostgreSQL")
        pytest.skip("Run the isolated integration gate for PostgreSQL")
    parts = urlsplit(url)
    assert parts.path == "/queue_recovery"
    name = "auth_limit_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        output = tmp_path / "result.json"
        db_url = urlunsplit(parts._replace(scheme="postgresql+asyncpg", path="/" + name))
        env = dict(os.environ, DATABASE_URL=db_url, AUTH_LIMIT_DATABASE_URL=db_url,
                   AUTH_LIMIT_RESULT=str(output))
        result = await asyncio.to_thread(
            _run_driver,
            [sys.executable, "-m", "tests.integration.auth." + driver],
            cwd=Path(__file__).resolve().parents[3], env=env,
        )
        (tmp_path / "driver.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(json.loads(output.read_text())["cases"]) == expected_cases
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
