"""Dedicated real databases and CLI terminals; no production credentials."""

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


def run_driver(tmp_path, db_url):
    output = tmp_path / "result.json"
    env = dict(os.environ, DATABASE_URL=db_url, AUTH_ENROLLMENT_DATABASE_URL=db_url,
               AUTH_ENROLLMENT_RESULT=str(output), APP_ROLE="web", QUEUE_BACKEND="memory",
               AUTH_ENABLED="true", API_KEY="", DB_MIGRATE_ON_STARTUP="true",
               POSTER_CACHE_DIR=str(tmp_path / "posters"), LLM_API_KEY="", TMDB_API_KEY="",
               BANGUMI_API_KEY="", WIGOLO_API_TOKEN="")
    command = [sys.executable, "-m", "tests.integration.security.auth_enrollment_driver"]
    with subprocess.Popen(command, cwd=Path(__file__).resolve().parents[3], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
    (tmp_path / "driver.log").write_text(stdout + stderr)
    assert process.returncode == 0, stdout + stderr
    assert len(json.loads(output.read_text())["cases"]) == 5


async def test_auth_enrollment_turso(tmp_path):
    await asyncio.to_thread(run_driver, tmp_path, f"sqlite+aioturso:///{tmp_path}/auth_enrollment_test.db")


async def test_auth_enrollment_postgres(tmp_path):
    url = os.environ.get("QUEUE_RECOVERY_POSTGRES_URL")
    if not url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Enrollment gate requires dedicated PostgreSQL")
        pytest.skip("Run isolated integration for PostgreSQL enrollment")
    parts = urlsplit(url)
    assert parts.path == "/queue_recovery"
    name = "auth_enrollment_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        db_url = urlunsplit(parts._replace(scheme="postgresql+asyncpg", path="/" + name))
        await asyncio.to_thread(run_driver, tmp_path, db_url)
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
