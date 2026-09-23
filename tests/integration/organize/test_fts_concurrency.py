"""Native crash containment and final index integrity under concurrent callers."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path


async def test_concurrent_sidecar_operations_preserve_index(tmp_path):
    output = tmp_path / "result.json"
    root = Path(__file__).resolve().parents[3]
    env = dict(
        os.environ,
        DATABASE_URL=f"sqlite+aioturso:///{tmp_path / 'main.db'}",
        PROBE_ROOT=str(tmp_path),
        PROBE_RESULT_PATH=str(output),
        RUST_BACKTRACE="full",
        PYTHONFAULTHANDLER="1",
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.integration.organize.fts_concurrency_driver"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(output.read_text())
    assert data["successful"] == {"write": 400, "read": 400}
    assert data["errors"] == {}
    assert data["final_ids"] == [f"synthetic-{i}" for i in range(10)]
