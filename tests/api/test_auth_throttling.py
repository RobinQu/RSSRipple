"""Local ASGI necessity probe; no production secret or network requests."""
from tests.api import test_auth

auth_client = test_auth.auth_client


async def test_repeated_invalid_otp_is_bounded(auth_client, monkeypatch):
    calls = []

    def reject(secret, code):
        calls.append(code)
        return False

    monkeypatch.setattr("app.api.v1.auth.verify_totp", reject)
    responses = [await auth_client.post("/api/v1/auth/otp", json={"code": "000000"})
                 for _ in range(20)]
    assert any(r.status_code == 429 for r in responses), {
        "statuses": [r.status_code for r in responses], "verification_calls": len(calls),
    }
    assert len(calls) < 20


async def test_budget_survives_401_and_returns_retry_after(auth_client, monkeypatch):
    from datetime import datetime, timedelta

    now = datetime(2026, 9, 23, 12)
    monkeypatch.setattr("app.services.auth_rate_limit.utcnow", lambda: now)
    monkeypatch.setattr("app.api.v1.auth.verify_totp", lambda *_: False)
    for _ in range(5):
        assert (await auth_client.post("/api/v1/auth/otp", json={"code": "000000"})).status_code == 401
    now += timedelta(seconds=20)
    denied = await auth_client.post("/api/v1/auth/otp", json={"code": "000000"})
    assert denied.status_code == 429
    assert denied.headers["Retry-After"] == "40"
    assert denied.json()["error"]["code"] == "RATE_LIMITED"
    now += timedelta(seconds=40)
    assert (await auth_client.post("/api/v1/auth/otp", json={"code": "000000"})).status_code == 401


async def test_forged_forwarded_header_does_not_reset_budget(auth_client, monkeypatch):
    monkeypatch.setattr("app.api.v1.auth.verify_totp", lambda *_: False)
    statuses = []
    for i in range(6):
        response = await auth_client.post(
            "/api/v1/auth/otp", json={"code": "000000"},
            headers={"X-Forwarded-For": f"192.0.2.{i + 1}"},
        )
        statuses.append(response.status_code)
    assert statuses == [401] * 5 + [429]
    assert (await auth_client.get("/api/v1/auth/status")).status_code == 200
    assert (await auth_client.post("/api/v1/auth/logout")).status_code == 200


async def test_successful_codes_also_consume_budget(auth_client, db_session):
    import pyotp

    from app.services.auth_service import AUTH_COOKIE_NAME, get_or_create_totp_secret

    secret = await get_or_create_totp_secret(db_session)
    await db_session.commit()
    for _ in range(5):
        response = await auth_client.post("/api/v1/auth/otp", json={"code": pyotp.TOTP(secret).now()})
        assert response.status_code == 200
        assert AUTH_COOKIE_NAME in response.cookies
        assert "HttpOnly" in response.headers["set-cookie"]
    denied = await auth_client.post("/api/v1/auth/otp", json={"code": pyotp.TOTP(secret).now()})
    assert denied.status_code == 429
    assert "set-cookie" not in denied.headers


async def test_schema_rejections_do_not_consume_verification_budget(auth_client, monkeypatch):
    monkeypatch.setattr("app.api.v1.auth.verify_totp", lambda *_: False)
    for _ in range(6):
        assert (await auth_client.post("/api/v1/auth/otp", json={"code": {}})).status_code == 422
    assert (await auth_client.post("/api/v1/auth/otp", json={"code": "not-six-digits"})).status_code == 401


async def test_database_failure_never_verifies_otp(auth_client, db_session_factory, monkeypatch):
    from unittest.mock import Mock

    from httpx import ASGITransport, AsyncClient

    from tests.api.conftest import _build_test_app

    verification = Mock(return_value=True)
    monkeypatch.setattr("app.api.v1.auth.verify_totp", verification)
    monkeypatch.setattr("app.database.async_session_factory", Mock(side_effect=RuntimeError("budget unavailable")))
    app = _build_test_app(db_session_factory, with_auth=True)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
    ) as client:
        response = await client.post("/api/v1/auth/otp", json={"code": "000000"})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert "set-cookie" not in response.headers
    verification.assert_not_called()
