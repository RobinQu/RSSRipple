"""Real startup, terminal enrollment, and login against a dedicated database."""

import asyncio
import errno
import hashlib
import io
import json
import logging
import os
import pty
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pyotp
from sqlalchemy import select

from app import database, main
from app.models.app_setting import AppSetting
from app.services.auth_service import (
    AUTH_COOKIE_NAME,
    SETTING_COOKIE_SECRET,
    SETTING_TOTP_SECRET,
    make_cookie,
    validate_cookie,
)


def terminal_command(*args):
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.scripts.auth_enrollment", *args],
            stdout=slave, stderr=subprocess.PIPE, timeout=20,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    try:
        chunks = []
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            if not chunk:
                break
            chunks.append(chunk)
        return result.returncode, b"".join(chunks).decode().strip(), result.stderr.decode()
    finally:
        os.close(master)


async def settings_snapshot():
    async with database.async_session_factory() as db:
        return {row.key: row.value for row in (await db.execute(select(AppSetting))).scalars()}


def snapshot_hash(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


async def bootstrap():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("app")
    logger.addHandler(handler)
    try:
        async with main.lifespan(main.app):
            pass
        before = await settings_snapshot()
        secret, cookie_secret = before[SETTING_TOTP_SECRET], before[SETTING_COOKIE_SECRET]
        cookie = make_cookie(cookie_secret)
        logs = stream.getvalue()
        assert "otpauth://" not in logs and secret not in logs and cookie_secret not in logs
        # Only the dedicated test coordinator receives this synthetic cookie.
        print(json.dumps({"settings_hash": snapshot_hash(before), "cookie": cookie}))
    finally:
        logger.removeHandler(handler)
        await database.engine.dispose()


async def run():
    output = Path(os.environ["AUTH_ENROLLMENT_RESULT"])
    starts = []
    # Stop the actual owning process before opening Turso from another one;
    # disposing SQLAlchemy alone need not release the embedded engine handle.
    for _ in range(2):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "tests.integration.security.auth_enrollment_driver", "bootstrap"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        starts.append(json.loads(result.stdout))
    assert starts[0]["settings_hash"] == starts[1]["settings_hash"]
    try:
        code, uri, stderr = await asyncio.to_thread(terminal_command, "--show")
        assert code == 0, stderr
        authenticator = pyotp.parse_uri(uri)
        secret = authenticator.secret
        assert secret not in stderr
        refused = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-m", "app.scripts.auth_enrollment", "--show"],
            capture_output=True, text=True, timeout=20,
        )
        assert refused.returncode == 2 and refused.stdout == ""
        assert secret not in refused.stderr
        missing_flag, displayed, error = await asyncio.to_thread(terminal_command)
        assert missing_flag == 2 and displayed == "" and secret not in error
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post("/api/v1/auth/otp", json={"code": authenticator.now()})
            assert response.status_code == 200, response.status_code
            assert AUTH_COOKIE_NAME in response.cookies
            assert (await client.get("/api/v1/auth/status")).json()["data"]["authenticated"] is True
        after = await settings_snapshot()
        assert after[SETTING_TOTP_SECRET] == secret
        assert snapshot_hash(after) == starts[0]["settings_hash"]
        assert validate_cookie(starts[0]["cookie"], after[SETTING_COOKIE_SECRET])
        output.write_text(json.dumps({"cases": [
            "fresh-and-repeated-startup-no-secret-log", "real-terminal-existing-enrollment",
            "redirected-output-refused", "missing-show-refused", "enrolled-otp-login-and-existing-cookie",
        ]}, indent=2))
    finally:
        await database.engine.dispose()


if __name__ == "__main__":
    url = os.environ["AUTH_ENROLLMENT_DATABASE_URL"]
    assert url == os.environ["DATABASE_URL"]
    assert Path(urlsplit(url).path).name.startswith("auth_enrollment_")
    asyncio.run(bootstrap() if sys.argv[1:] == ["bootstrap"] else run())
