"""Enrollment reads the existing test credentials without changing authentication."""

from unittest.mock import AsyncMock

import pyotp
import pytest
from sqlalchemy import select

from app import database
from app.models.app_setting import AppSetting
from app.scripts import auth_enrollment as enrollment
from app.services.auth_service import (
    get_or_create_cookie_secret,
    get_or_create_totp_secret,
    make_cookie,
    validate_cookie,
    verify_totp,
)


async def test_enrollment_preserves_credentials_and_existing_cookie(db_engine):
    async with database.async_session_factory() as db:
        secret = await get_or_create_totp_secret(db)
        cookie_secret = await get_or_create_cookie_secret(db)
        await db.commit()
        before = {row.key: row.value for row in (await db.execute(select(AppSetting))).scalars()}
    cookie = make_cookie(cookie_secret)
    for _ in range(2):
        uri = await enrollment._read_uri()
        authenticator = pyotp.parse_uri(uri)
        assert authenticator.secret == secret
        assert verify_totp(secret, authenticator.now())
    async with database.async_session_factory() as db:
        after = {row.key: row.value for row in (await db.execute(select(AppSetting))).scalars()}
    assert before == after
    assert validate_cookie(cookie, cookie_secret)


async def test_uninitialized_enrollment_does_not_create_credentials(db_engine):
    assert await enrollment._read_uri() is None
    async with database.async_session_factory() as db:
        assert (await db.execute(select(AppSetting))).scalars().all() == []


def test_explicit_show_required(capsys):
    with pytest.raises(SystemExit) as error:
        enrollment.main([])
    assert error.value.code == 2
    assert "--show is required" in capsys.readouterr().err


def test_redirected_output_refused_before_database_access(monkeypatch, capsys):
    read = AsyncMock()
    monkeypatch.setattr(enrollment, "_read_uri", read)
    monkeypatch.setattr(enrollment.sys.stdout, "isatty", lambda: False)
    assert enrollment.main(["--show"]) == 2
    read.assert_not_called()
    assert "redirected output is refused" in capsys.readouterr().err


def test_connection_error_does_not_print_credentials(monkeypatch, capsys):
    monkeypatch.setattr(enrollment.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(enrollment, "_run", AsyncMock(side_effect=RuntimeError("postgres://synthetic-secret")))
    assert enrollment.main(["--show"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "synthetic-secret" not in output.err
    assert "Cannot read authentication settings" in output.err
