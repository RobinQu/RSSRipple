"""Legacy upgrade must preserve data and enforce dispatch identity in the DB."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path


async def test_dispatch_schema_upgrade_is_repeatable(tmp_path):
    output = tmp_path / "result.json"
    env = dict(
        os.environ,
        PROBE_ROOT=str(tmp_path),
        PROBE_RESULT_PATH=str(output),
        DATABASE_URL=f"sqlite+aioturso:///{tmp_path / 'legacy.db'}",
    )
    env.pop("PROBE_POSTGRES_PROJECT", None)
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.integration.organize.download_dispatch_schema_driver"],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["startup_calls"] == 2
    assert report["legacy_row_preserved"] and report["reservation_row_preserved"]
    assert report["raw_default_settled"] is False
    assert report["rejected"] == ["operation", "task", "null-settled"]
