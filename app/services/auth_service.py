"""Authentication service — TOTP secrets, session cookies, and API keys.

Secrets that must persist across restarts but are generated on first use live
in the ``app_settings`` table (via :mod:`app.services.settings_service`):

- ``auth_totp_secret``: base32 TOTP secret for the single admin user.
- ``auth_cookie_secret``: HMAC key for signing session cookies.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import pyotp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_key import ApiKey
from app.services.settings_service import get_setting, set_setting

AUTH_COOKIE_NAME = "rssripple_auth"
COOKIE_MAX_AGE_DAYS = 30

SETTING_TOTP_SECRET = "auth_totp_secret"
SETTING_COOKIE_SECRET = "auth_cookie_secret"

_API_KEY_PREFIX = "rr_"


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------

async def get_or_create_totp_secret(db: AsyncSession) -> str:
    """Return the base32 TOTP secret, generating and persisting one on first use."""
    secret = await get_setting(db, SETTING_TOTP_SECRET)
    if secret:
        return secret
    secret = pyotp.random_base32()
    await set_setting(db, SETTING_TOTP_SECRET, secret)
    await db.flush()
    return secret


def totp_provisioning_uri(secret: str) -> str:
    """Return an ``otpauth://`` URI for importing into an authenticator app."""
    return pyotp.TOTP(secret).provisioning_uri(name="admin", issuer_name="RSSRipple")


def verify_totp(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code, tolerating one window of clock drift."""
    return bool(pyotp.TOTP(secret).verify(code, valid_window=1))


# ---------------------------------------------------------------------------
# Session cookie
#
# Value format: ``{expiry_unix_ts}.{hmac_sha256_hex(expiry_ts, cookie_secret)}``.
# The signature is checked with a constant-time compare and the expiry against
# the current time.
# ---------------------------------------------------------------------------

async def get_or_create_cookie_secret(db: AsyncSession) -> str:
    """Return the cookie HMAC secret, generating and persisting one on first use."""
    secret = await get_setting(db, SETTING_COOKIE_SECRET)
    if secret:
        return secret
    secret = secrets.token_urlsafe(32)
    await set_setting(db, SETTING_COOKIE_SECRET, secret)
    await db.flush()
    return secret


def make_cookie(secret: str, max_age_days: int = COOKIE_MAX_AGE_DAYS) -> str:
    """Create a signed session cookie value valid for *max_age_days* days."""
    expiry = int(time.time()) + max_age_days * 86400
    payload = str(expiry)
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def validate_cookie(value: str | None, secret: str) -> bool:
    """Whether *value* is a well-formed, correctly signed, unexpired cookie."""
    if not value:
        return False
    try:
        payload, sig = value.split(".", 1)
        expiry = int(payload)
    except (ValueError, AttributeError):
        return False
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    return expiry > int(time.time())


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def hash_api_key(plaintext: str) -> str:
    """Return the SHA-256 hex digest of a presented API key."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


async def create_api_key(db: AsyncSession, name: str) -> tuple[ApiKey, str]:
    """Create a new API key. Returns ``(row, plaintext)`` — the plaintext is
    only available here; only its SHA-256 digest and display prefix are stored."""
    plaintext = _API_KEY_PREFIX + secrets.token_urlsafe(32)
    row = ApiKey(name=name, prefix=plaintext[:10], key_hash=hash_api_key(plaintext))
    db.add(row)
    await db.flush()
    return row, plaintext


async def check_api_key(db: AsyncSession, presented: str) -> bool:
    """Whether *presented* matches a stored API key (constant-work lookup)."""
    stmt = select(ApiKey.id).where(ApiKey.key_hash == hash_api_key(presented))
    return (await db.execute(stmt)).first() is not None
