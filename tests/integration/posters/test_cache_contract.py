"""Public static-file contract, damaged receipts and real process concurrency."""
import asyncio
import hashlib
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from app.services.metadata_service import download_and_cache_poster
from tests.integration.posters.test_cache_publication import BODY_SHA
from tests.integration.posters.test_cache_publication import recorded_poster as recorded_poster


@pytest.mark.parametrize("receipt", [None, "not-json", "[]", '{"version":2}', '{"version":1,"size":147157,"sha256":"bad"}'])
async def test_damaged_receipt_forces_refetch(recorded_poster, receipt):
    state = recorded_poster
    assert await download_and_cache_poster(state.url)
    proof = state.cache / f".{state.digest}.jpg.json"
    if receipt is None:
        proof.unlink()
    else:
        proof.write_text(receipt)
    assert await download_and_cache_poster(state.url)
    assert state.hits == 2
    assert hashlib.sha256(state.final.read_bytes()).hexdigest() == BODY_SHA
    assert json.loads(proof.read_text())["sha256"] == BODY_SHA


async def test_static_response_serves_complete_jpeg_with_matching_mime(recorded_poster):
    state = recorded_poster
    url = await download_and_cache_poster(state.url)
    app = FastAPI()
    app.mount("/posters", StaticFiles(directory=state.cache), name="posters")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
        response = await client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert int(response.headers["content-length"]) == len(state.body)
    assert hashlib.sha256(response.content).hexdigest() == BODY_SHA
    proof = (state.cache / f".{state.digest}.jpg.json").read_text()
    assert state.url not in proof and state.origin not in proof


async def test_two_real_processes_publish_one_complete_cache(recorded_poster):
    state = recorded_poster
    state.barrier = threading.Barrier(2)
    processes = []
    try:
        for _ in range(2):
            processes.append(await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tests.integration.posters.poster_crash_driver",
                "success", state.url, str(state.cache), cwd=Path(__file__).parents[3],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            ))
        results = await asyncio.wait_for(asyncio.gather(*(p.communicate() for p in processes)), timeout=30)
        for process, (stdout, stderr) in zip(processes, results, strict=True):
            assert process.returncode == 0, (stdout, stderr)
            assert stdout.decode().strip() == f"/posters/{state.digest}.jpg"
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
        await asyncio.gather(*(p.wait() for p in processes))
    assert state.hits == 2
    assert hashlib.sha256(state.final.read_bytes()).hexdigest() == BODY_SHA
    assert not list(state.cache.glob("*.part"))
    assert await download_and_cache_poster(state.url) == f"/posters/{state.digest}.jpg"
    assert state.hits == 2


async def test_replacement_preserves_existing_file_permissions(recorded_poster):
    state = recorded_poster
    state.cache.mkdir()
    state.final.write_bytes(state.body[:4])
    state.final.chmod(0o600)
    assert await download_and_cache_poster(state.url)
    assert state.final.stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(state.final.read_bytes()).hexdigest() == BODY_SHA


async def test_new_publication_respects_process_umask(recorded_poster):
    state = recorded_poster
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tests.integration.posters.poster_crash_driver",
        "success", state.url, str(state.cache), "--umask", "027",
        cwd=Path(__file__).parents[3],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        assert process.returncode == 0, (stdout, stderr)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert state.final.stat().st_mode & 0o777 == 0o640
    assert hashlib.sha256(state.final.read_bytes()).hexdigest() == BODY_SHA
