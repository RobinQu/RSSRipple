"""Real API/database interleavings; required by the isolated integration gate."""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

CASES = {
    "shape_edit": {"EDITOR_API": "1", "EDITOR_SHAPE": "1"},
    "edit_during_lookup": {"EDITOR_API": "1"},
    "existing_mapping_edit": {"EDITOR_API": "1", "PREEXISTING_MAPPING": "1"},
    "cached_result_edit": {"EDITOR_API": "1", "SHORTCUT": "cache"},
    "known_work_edit": {"EDITOR_API": "1", "SHORTCUT": "known"},
    "edit_after_lock": {"EDITOR_API": "1", "LATE_MAPPING": "1", "REVERSE_API": "1"},
    "merge_during_lookup": {"MERGE_API": "1", "PREEXISTING_MAPPING": "1"},
    "merge_before_write": {"MERGE_API": "1", "PREEXISTING_MAPPING": "1", "LATE_MAPPING": "1", "REVERSE_API": "1"},
    "unique_error_propagates": {"MERGE_API": "1", "PREEXISTING_MAPPING": "1", "LATE_MAPPING": "1", "REVERSE_API": "1", "INJECT_UNIQUE": "1"},
}


@pytest.mark.parametrize("case", CASES)
async def test_manual_mapping_concurrency(tmp_path, case):
    admin_url = os.environ.get("QUEUE_RECOVERY_POSTGRES_URL")
    if not admin_url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Manual mapping gate requires dedicated PostgreSQL")
        pytest.skip("Run the isolated integration gate for PostgreSQL interleavings")
    parts = urlsplit(admin_url)
    assert parts.path == "/queue_recovery", "Refuse a non-test admin database"
    name = "manual_mapping_" + uuid.uuid4().hex
    admin = await asyncpg.connect(admin_url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        await _run_case(tmp_path, case, urlunsplit(parts._replace(scheme="postgresql+asyncpg", path="/" + name)))
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _run_case(tmp_path, case, database_url):
    env = dict(os.environ)
    for key in ("EDITOR_API", "EDITOR_SHAPE", "PREEXISTING_MAPPING", "SHORTCUT", "MERGE_API", "LATE_MAPPING", "REVERSE_API", "INJECT_UNIQUE", "MAPPING_ONLY"):
        env.pop(key, None)
    env.update(CASES[case])
    env.update(DATABASE_URL=database_url,
               RECORDED_TITLE="1")
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.integration.metadata.manual_mapping_driver"],
        cwd=Path(__file__).resolve().parents[3], env=env,
        capture_output=True, text=True, timeout=45,
    )
    (tmp_path / "driver.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("case", ["shape_edit", "edit_during_lookup", "existing_mapping_edit", "cached_result_edit", "known_work_edit", "merge_during_lookup"])
async def test_manual_mapping_turso(tmp_path, case):
    await _run_case(tmp_path, case, "sqlite+aioturso:///" + str(tmp_path / "manual_mapping_test.db"))
