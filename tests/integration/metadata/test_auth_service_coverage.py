"""Auth service in-process coverage.

Covers the deterministic auth_service paths that the HTTP suite only reaches
through the middleware gate: cookie signing/validation, TOTP verification,
secret get-or-create with an existing value, and API-key lookup.
"""

from __future__ import annotations

import time
import uuid

import pyotp

from app.services.auth_service import (
    AUTH_COOKIE_NAME,
    check_api_key,
    create_api_key,
    get_or_create_cookie_secret,
    get_or_create_totp_secret,
    make_cookie,
    validate_cookie,
    verify_totp,
)


async def test_cookie_roundtrip():
    secret = "test-cookie-secret"
    value = make_cookie(secret)
    assert "." in value
    assert validate_cookie(value, secret) is True
    # Wrong secret → invalid.
    assert validate_cookie(value, "other-secret") is False
    # Garbage / malformed values → invalid.
    assert validate_cookie(None, secret) is False
    assert validate_cookie("", secret) is False
    assert validate_cookie("no-dot", secret) is False
    assert validate_cookie("abc.xyz", secret) is False
    assert validate_cookie("notanint.sig", secret) is False


async def test_cookie_expiry():
    secret = "test-cookie-secret"
    expiry = int(time.time()) - 10  # already past
    payload = str(expiry)
    import hashlib
    import hmac

    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert validate_cookie(f"{payload}.{sig}", secret) is False


async def test_totp_verification():
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()
    assert verify_totp(secret, code) is True
    assert verify_totp(secret, "000000") is False


async def test_get_or_create_secrets(db_session):
    # Fresh secret is generated and persisted.
    s1 = await get_or_create_totp_secret(db_session)
    assert len(s1) >= 16
    # Second call returns the persisted one.
    s2 = await get_or_create_totp_secret(db_session)
    assert s1 == s2

    c1 = await get_or_create_cookie_secret(db_session)
    assert c1
    c2 = await get_or_create_cookie_secret(db_session)
    assert c1 == c2


async def test_api_key_lookup(db_session):
    row, plaintext = await create_api_key(db_session, f"auth-cov-{uuid.uuid4().hex[:6]}")
    assert (await check_api_key(db_session, plaintext)) is True
    assert (await check_api_key(db_session, "rr_wrong")) is False
    assert AUTH_COOKIE_NAME
    assert row.prefix == plaintext[:10]
