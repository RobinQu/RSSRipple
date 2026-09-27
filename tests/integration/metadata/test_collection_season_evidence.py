"""Mandatory PostgreSQL counterpart of the Turso season-evidence matrix."""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest


async def test_collection_season_evidence_postgres(tmp_path):
    url = os.environ.get('QUEUE_RECOVERY_POSTGRES_URL')
    if not url:
        if os.environ.get('QUEUE_RECOVERY_REQUIRED') == '1':
            pytest.fail('Season evidence gate requires dedicated PostgreSQL')
        pytest.skip('Run the isolated integration gate for PostgreSQL')
    parts = urlsplit(url)
    assert parts.path == '/queue_recovery'
    name = 'season_evidence_' + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        output = tmp_path / 'result.json'
        env = dict(os.environ,
                   SEASON_EVIDENCE_DATABASE_URL=urlunsplit(parts._replace(scheme='postgresql+asyncpg', path='/' + name)),
                   SEASON_EVIDENCE_RESULT=str(output))
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, '-m', 'tests.integration.metadata.season_evidence_driver'],
            cwd=Path(__file__).resolve().parents[3], env=env,
            capture_output=True, text=True, timeout=120)
        (tmp_path / 'driver.log').write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(json.loads(output.read_text())['cases']) == 38
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
