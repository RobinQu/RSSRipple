"""Real producer transactions with captured corpus evidence in fresh processes."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("failure,queue_recovery", [("", False), ("created", False), ("metadata", False), ("", True)])
@pytest.mark.parametrize("isolation", ["DEFERRED", "CONCURRENT"])
async def test_feed_publication_atomicity_and_consumption(
    failure, queue_recovery, isolation, tmp_path, record_testsuite_property,
):
    output = tmp_path / "result.json"
    env = dict(
        os.environ,
        DATABASE_URL=f"sqlite+aioturso:///{tmp_path / 'publication-feed.db'}?isolation_level={isolation}",
        FAIL_KIND=failure,
        QUEUE_RECOVERY="1" if queue_recovery else "0",
        PROBE_RESULT_PATH=str(output),
    )
    root = Path(__file__).resolve().parents[3]
    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.integration.organize.publication_feed_driver"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(output.read_text())
    assert result["failure"] == (failure or None)
    assert result["lost_wakeup_recovered_by_live_queue"] == queue_recovery
    assert result["consumed"]["dispatched"] == (0 if failure == "created" else 1)
    assert result["next_run"]["total_resources"] == 0
    record_testsuite_property("real_case", result["case"])
    record_testsuite_property("torrent_sha256", result["torrent_sha256"])
    record_testsuite_property("rss_source", result["rss"])
    record_testsuite_property("metadata_boundary", result["metadata"])
