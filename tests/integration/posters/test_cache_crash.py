"""Actual process termination cannot turn a staged JPEG into a cache hit."""
import asyncio
import hashlib
import sys
from pathlib import Path

import pytest

from app.services.metadata_service import download_and_cache_poster
from tests.integration.posters.test_cache_publication import BODY_SHA
from tests.integration.posters.test_cache_publication import recorded_poster as recorded_poster


@pytest.mark.parametrize("phase,code", [("stage", 97), ("published", 98)])
async def test_actual_process_crash_recovers_complete_poster(recorded_poster, phase, code):
    state = recorded_poster
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tests.integration.posters.poster_crash_driver",
        phase, state.url, str(state.cache),
        cwd=Path(__file__).parents[3],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        assert process.returncode == code, (stdout, stderr)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if phase == "stage":
        assert not state.final.exists()
    else:
        assert hashlib.sha256(state.final.read_bytes()).hexdigest() == BODY_SHA
    abandoned = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in state.cache.glob("*.part")}
    assert abandoned, "A terminated writer really left staging data"
    assert await download_and_cache_poster(state.url) == f"/posters/{state.digest}.jpg"
    assert hashlib.sha256(state.final.read_bytes()).hexdigest() == BODY_SHA
    assert state.hits == 2
    # Recovery must never delete another writer's staging files by a glob.
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in state.cache.glob("*.part")} == abandoned
    assert await download_and_cache_poster(state.url) == f"/posters/{state.digest}.jpg"
    assert state.hits == 2
