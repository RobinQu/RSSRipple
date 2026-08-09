"""API tests for the AuthMiddleware and the /auth endpoints.

Unlike most API tests (stripped app, no middleware), these build the test app
WITH the production AuthMiddleware and point its DB access at the per-test
session factory.
"""

from __future__ import annotations

import pyotp
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.services.auth_service import (
    AUTH_COOKIE_NAME,
    create_api_key,
    get_or_create_cookie_secret,
    get_or_create_totp_secret,
    make_cookie,
)
from tests.api.conftest import _build_test_app


@pytest_asyncio.fixture
async def auth_client(db_session_factory, monkeypatch):
    """HTTP client against a test app WITH AuthMiddleware installed."""
    # The middleware opens sessions via app.database.async_session_factory;
    # retarget it at the per-test engine.
    import app.database as db_mod

    monkeypatch.setattr(db_mod, "async_session_factory", db_session_factory)
    # Isolate from any API_KEY / AUTH_ENABLED in the developer's .env.
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "auth_enabled", True)

    test_app = _build_test_app(db_session_factory, with_auth=True)
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestMiddlewareGating:
    async def test_no_credentials_401(self, auth_client):
        res = await auth_client.get("/api/v1/dashboard")
        assert res.status_code == 401
        body = res.json()
        assert body["success"] is False
        assert body["data"] is None
        assert body["error"]["code"] == "UNAUTHORIZED"

    async def test_wrong_api_key_401(self, auth_client, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "static-secret")
        res = await auth_client.get(
            "/api/v1/dashboard", headers={"Authorization": "Bearer nope"}
        )
        assert res.status_code == 401

    async def test_static_key_bearer(self, auth_client, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "static-secret")
        res = await auth_client.get(
            "/api/v1/dashboard", headers={"Authorization": "Bearer static-secret"}
        )
        assert res.status_code == 200

    async def test_static_key_x_api_key_header(self, auth_client, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "static-secret")
        res = await auth_client.get(
            "/api/v1/dashboard", headers={"X-API-Key": "static-secret"}
        )
        assert res.status_code == 200

    async def test_db_api_key(self, auth_client, db_session):
        _, plaintext = await create_api_key(db_session, "ops")
        await db_session.commit()
        res = await auth_client.get(
            "/api/v1/dashboard", headers={"Authorization": f"Bearer {plaintext}"}
        )
        assert res.status_code == 200

    async def test_posters_gated(self, auth_client):
        # The file does not exist — but auth runs before the static mount,
        # so we get 401, not 404.
        res = await auth_client.get("/posters/whatever.png")
        assert res.status_code == 401
        assert res.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_auth_paths_exempt(self, auth_client):
        res = await auth_client.get("/api/v1/auth/status")
        assert res.status_code == 200
        assert res.json()["data"]["authenticated"] is False

    async def test_non_api_path_open(self, auth_client):
        res = await auth_client.get("/openapi.json")
        assert res.status_code == 200

    async def test_auth_disabled_passthrough(self, auth_client, monkeypatch):
        monkeypatch.setattr(settings, "auth_enabled", False)
        res = await auth_client.get("/api/v1/dashboard")
        assert res.status_code == 200


class TestOtpLogin:
    async def test_otp_login_issues_cookie_and_authenticates(self, auth_client, db_session):
        secret = await get_or_create_totp_secret(db_session)
        await db_session.commit()
        code = pyotp.TOTP(secret).now()

        res = await auth_client.post("/api/v1/auth/otp", json={"code": code})
        assert res.status_code == 200
        assert res.json()["data"]["authenticated"] is True
        assert AUTH_COOKIE_NAME in res.cookies

        # The issued cookie now authenticates API requests.
        authed = await auth_client.get("/api/v1/dashboard")
        assert authed.status_code == 200

        status = await auth_client.get("/api/v1/auth/status")
        assert status.json()["data"]["authenticated"] is True

    async def test_otp_wrong_code_401(self, auth_client, db_session, monkeypatch):
        await get_or_create_totp_secret(db_session)
        await db_session.commit()
        monkeypatch.setattr("app.api.v1.auth.verify_totp", lambda s, c: False)
        res = await auth_client.post("/api/v1/auth/otp", json={"code": "000000"})
        assert res.status_code == 401
        assert res.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_logout_clears_cookie(self, auth_client, db_session):
        secret = await get_or_create_totp_secret(db_session)
        await db_session.commit()
        code = pyotp.TOTP(secret).now()
        await auth_client.post("/api/v1/auth/otp", json={"code": code})

        res = await auth_client.post("/api/v1/auth/logout")
        assert res.status_code == 200
        assert res.json()["data"]["authenticated"] is False
        # The cookie jar no longer holds a usable cookie.
        assert auth_client.cookies.get(AUTH_COOKIE_NAME) in (None, "")


class TestCookieValidation:
    async def test_tampered_cookie_401(self, auth_client, db_session):
        await get_or_create_cookie_secret(db_session)
        await db_session.commit()
        auth_client.cookies.set(AUTH_COOKIE_NAME, "9999999999.deadbeef")
        res = await auth_client.get("/api/v1/dashboard")
        assert res.status_code == 401

    async def test_expired_cookie_401(self, auth_client, db_session):
        secret = await get_or_create_cookie_secret(db_session)
        await db_session.commit()
        expired = make_cookie(secret, max_age_days=-1)
        auth_client.cookies.set(AUTH_COOKIE_NAME, expired)
        res = await auth_client.get("/api/v1/dashboard")
        assert res.status_code == 401

    async def test_valid_cookie_passes(self, auth_client, db_session):
        secret = await get_or_create_cookie_secret(db_session)
        await db_session.commit()
        auth_client.cookies.set(AUTH_COOKIE_NAME, make_cookie(secret))
        res = await auth_client.get("/api/v1/dashboard")
        assert res.status_code == 200

    async def test_status_with_api_key(self, auth_client, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "static-secret")
        res = await auth_client.get(
            "/api/v1/auth/status", headers={"X-API-Key": "static-secret"}
        )
        assert res.status_code == 200
        assert res.json()["data"]["authenticated"] is True
