"""Two real HTTP processes sharing a dedicated PostgreSQL OTP budget."""

import asyncio
import json
import os
import signal
import socket
import sys
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pyotp
from sqlalchemy import delete, update

import app.models  # noqa: F401
from app import database
from app.models.app_setting import AppSetting
from app.models.auth_rate_limit import AuthRateLimitBucket
from app.services.auth_service import AUTH_COOKIE_NAME, SETTING_COOKIE_SECRET, SETTING_TOTP_SECRET
from app.utils.time import utcnow

TEST_SECRET = "JBSWY3DPEHPK3PXP"


async def serve(port_file: Path):
    import uvicorn

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port_file.write_text(str(listener.getsockname()[1]))
    try:
        # Production router/middleware/handlers, with startup already covered
        # separately; avoid starting schedulers or touching any external stack.
        config = uvicorn.Config("app.main:app", lifespan="off", proxy_headers=False,
                                log_level="error", access_log=False)
        await uvicorn.Server(config).serve(sockets=[listener])
    finally:
        listener.close()
        await database.engine.dispose()


async def start_server(directory: Path, index: int, processes: list):
    port_file = directory / f"http-port-{index}"
    env = dict(os.environ, AUTH_ENABLED="true", API_KEY="", APP_ROLE="web",
               SCHEDULER_ENABLED="false", LLM_API_KEY="", TMDB_API_KEY="",
               BANGUMI_API_KEY="", WIGOLO_API_TOKEN="")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tests.integration.auth.rate_limit_http_driver", "serve", str(port_file),
        env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    processes.append(process)
    deadline = time.monotonic() + 20
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        while time.monotonic() < deadline:
            assert process.returncode is None, (await process.communicate())[1].decode()
            if port_file.exists():
                url = f"http://127.0.0.1:{port_file.read_text()}"
                try:
                    response = await client.get(url + "/api/v1/auth/status")
                    if response.status_code == 200:
                        return process, url
                except httpx.TransportError:
                    pass
            await asyncio.sleep(0.02)
    raise TimeoutError("Dedicated HTTP test process did not become ready")


async def stop_server(process):
    requested_stop = process.returncode is None
    if requested_stop:
        process.terminate()
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    # Uvicorn restores and re-raises SIGTERM after its graceful shutdown.
    # Only a signal sent by this helper is an expected signal exit.
    expected = (0, -signal.SIGTERM) if requested_stop else (0,)
    assert process.returncode in expected, (process.returncode, stderr.decode())


async def clear_budget():
    async with database.async_session_factory() as db, db.begin():
        await db.execute(delete(AuthRateLimitBucket))


async def coordinator():
    output = Path(os.environ["AUTH_LIMIT_RESULT"])
    async with database.engine.begin() as connection:
        await connection.run_sync(database.Base.metadata.create_all)
    async with database.async_session_factory() as db, db.begin():
        db.add_all([
            AppSetting(key=SETTING_TOTP_SECRET, value=TEST_SECRET),
            AppSetting(key=SETTING_COOKIE_SECRET, value="synthetic-http-cookie-secret"),
        ])
    processes = []
    try:
        first, url1 = await start_server(output.parent, 0, processes)
        _, url2 = await start_server(output.parent, 1, processes)
        urls = [url1, url2]
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            responses = await asyncio.gather(*(
                client.post(urls[index % 2] + "/api/v1/auth/otp", json={"code": pyotp.TOTP(TEST_SECRET).now()})
                for index in range(12)
            ))
            statuses = [response.status_code for response in responses]
            assert statuses.count(200) == 5 and statuses.count(429) == 7, statuses
            cookie = next(response.cookies[AUTH_COOKIE_NAME] for response in responses if response.status_code == 200)
            client.cookies.set(AUTH_COOKIE_NAME, cookie)
            assert (await client.get(url2 + "/api/v1/auth/status")).json()["data"]["authenticated"] is True
            await stop_server(first)
            _, replacement = await start_server(output.parent, 2, processes)
            denied = await client.post(replacement + "/api/v1/auth/otp", json={"code": pyotp.TOTP(TEST_SECRET).now()})
            assert denied.status_code == 429
            assert int(denied.headers["Retry-After"]) > 0
            assert "set-cookie" not in denied.headers
            assert (await client.get(replacement + "/api/v1/auth/status")).json()["data"]["authenticated"] is True
            urls = [replacement, url2]
            await clear_budget()

            async def request_peer(index):
                transport = httpx.AsyncHTTPTransport(local_address=f"127.0.0.{index + 2}")
                async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=10) as peer:
                    return await asyncio.gather(*(
                        peer.post(urls[attempt % 2] + "/api/v1/auth/otp", json={"code": pyotp.TOTP(TEST_SECRET).now()})
                        for attempt in range(5)
                    ))

            batches = await asyncio.gather(*(request_peer(index) for index in range(8)))
            statuses = [response.status_code for batch in batches for response in batch]
            assert statuses.count(200) == 30 and statuses.count(429) == 10, statuses
            await clear_budget()
            for _ in range(5):
                assert (await client.post(url2 + "/api/v1/auth/otp", json={"code": "000000"})).status_code == 401
            assert (await client.post(replacement + "/api/v1/auth/otp", json={"code": pyotp.TOTP(TEST_SECRET).now()})).status_code == 429
            async with database.async_session_factory() as db, db.begin():
                await db.execute(update(AuthRateLimitBucket).values(resets_at=utcnow() - timedelta(seconds=1)))
            assert (await client.post(replacement + "/api/v1/auth/otp", json={"code": pyotp.TOTP(TEST_SECRET).now()})).status_code == 200
        output.write_text(json.dumps({"cases": [
            "http-concurrent-peer-budget", "http-cookie-and-budget-survive-process-replacement",
            "http-global-budget-across-eight-peers", "http-invalid-attempts-and-expiry-recovery",
        ]}, indent=2))
    finally:
        try:
            results = await asyncio.gather(
                *(stop_server(process) for process in processes if process.returncode is None),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, BaseException)]
            assert not errors, errors
        finally:
            await database.engine.dispose()


async def main():
    url = os.environ["AUTH_LIMIT_DATABASE_URL"]
    assert url == os.environ["DATABASE_URL"]
    assert urlsplit(url).path.startswith("/auth_limit_")
    if len(sys.argv) == 3 and sys.argv[1] == "serve":
        await serve(Path(sys.argv[2]))
    else:
        await coordinator()


if __name__ == "__main__":
    asyncio.run(main())
