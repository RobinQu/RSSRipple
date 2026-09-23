"""Real process termination after a committed magnet claim; no live network."""

import asyncio
import hashlib
import os
import signal
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock


async def claim_and_wait(resource_id):
    from app.services import magnet_resolve as mr

    async def wait_after_claim(resource_id, attempt_id):
        print(attempt_id, flush=True)
        await asyncio.Event().wait()

    mr.lt = object()
    mr.settings.magnet_resolve_enabled = True
    mr._attempt_loop = wait_after_claim
    assert await mr.launch_resolution(resource_id)
    await asyncio.gather(*mr._background_tasks)


async def verify_recovery(db, channel, patcher, cache_dir):
    from app import database
    from app.job_handlers import _handle_magnet_resolve_sweep
    from app.models.file_resource import FileResource
    from app.services import magnet_resolve as mr
    from app.services.torrent_inspect import parse_torrent_files
    from app.utils.time import utcnow
    from tests.unit.test_magnet_resolve import _make_magnet_resource

    resource = await _make_magnet_resource(db, channel.id)
    resource_id = resource.id
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tests.integration.queue_recovery.magnet_crash", resource_id,
        stdout=asyncio.subprocess.PIPE, env=dict(os.environ),
    )
    try:
        old_attempt = (await asyncio.wait_for(process.stdout.readline(), 15)).decode().strip()
        assert old_attempt
        async with database.async_session_factory() as observer:
            row = await observer.get(FileResource, resource_id)
            assert row.magnet_resolve_status == "pending"
            assert row.magnet_resolve_attempt_id == old_attempt
        process.kill()
        assert await asyncio.wait_for(process.wait(), 10) == -signal.SIGKILL
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    # Advance only the persisted test timestamp, not the application's clock.
    async with database.async_session_factory() as recovery:
        row = await recovery.get(FileResource, resource_id)
        row.magnet_resolve_updated_at = utcnow() - timedelta(days=30)
        await recovery.commit()
    enqueue = AsyncMock()
    patcher.setattr(mr, "enqueue_resolution", enqueue)
    result = await _handle_magnet_resolve_sweep({})
    assert result["reclaimed"] == 1
    enqueue.assert_any_await(resource_id)

    source = Path("tests/fixtures/metadata_corpus_v1/torrents/987a72c09d5b0c2e934fa5016cc4dda6427a80dc5a4594e284a06ccf966acdb2.torrent")
    content = source.read_bytes()
    assert parse_torrent_files(str(source))

    async def resolve(uri, destination, timeout, **kwargs):
        Path(destination).write_bytes(content)

    patcher.setattr(mr, "lt", object())
    patcher.setattr(mr.settings, "magnet_resolve_enabled", True)
    patcher.setattr(mr.settings, "torrent_cache_dir", str(cache_dir))
    patcher.setattr(mr, "resolve_magnet_to_cache", resolve)
    assert await mr.launch_resolution(resource_id)
    await asyncio.gather(*mr._background_tasks)
    async with database.async_session_factory() as observer:
        row = await observer.get(FileResource, resource_id)
        assert row.magnet_resolve_status == "done"
        assert row.magnet_resolve_attempt_id != old_attempt
        assert Path(row.torrent_file).read_bytes() == content
        assert parse_torrent_files(row.torrent_file)
        await observer.refresh(row, ["file_assignments"])
        assert len(row.file_assignments) == 1
        assert row.file_assignments[0].file_path == parse_torrent_files(str(source))[0]["name"]
    return {"case": "sigkill_after_claim", "passed": True, "worker_exit": process.returncode,
            "torrent_sha256": hashlib.sha256(content).hexdigest(),
            "network": "recorded torrent bytes; resolver mocked; actual single-file inspection",
            "persisted_file_assignments": 1}


if __name__ == "__main__":
    asyncio.run(claim_and_wait(sys.argv[1]))
