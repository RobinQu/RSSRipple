"""Tests for SecurityHeadersMiddleware: baseline headers, CSP profiles, HSTS flag."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.middleware.security_headers import SecurityHeadersMiddleware


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/ping")
    async def ping():
        return {"ok": True}

    @app.get("/some/spa/route")
    async def spa():
        return {"shell": True}

    app.add_middleware(SecurityHeadersMiddleware)
    return app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as ac:
        yield ac


class TestBaselineHeaders:
    async def test_api_response_has_security_headers(self, client):
        res = await client.get("/api/v1/ping")
        assert res.status_code == 200
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["x-frame-options"] == "DENY"
        assert res.headers["referrer-policy"] == "no-referrer"
        assert "content-security-policy" in res.headers

    async def test_hsts_absent_by_default(self, client, monkeypatch):
        monkeypatch.delenv("SECURITY_HSTS_ENABLED", raising=False)
        res = await client.get("/api/v1/ping")
        assert "strict-transport-security" not in res.headers

    async def test_hsts_present_when_enabled(self, client, monkeypatch):
        monkeypatch.setenv("SECURITY_HSTS_ENABLED", "true")
        res = await client.get("/api/v1/ping")
        assert res.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"

    async def test_hsts_falsy_values_ignored(self, client, monkeypatch):
        monkeypatch.setenv("SECURITY_HSTS_ENABLED", "0")
        res = await client.get("/api/v1/ping")
        assert "strict-transport-security" not in res.headers


class TestCspProfiles:
    async def test_api_and_spa_get_strict_self_csp(self, client):
        res = await client.get("/api/v1/ping")
        csp = res.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "jsdelivr" not in csp
        assert "unsafe-inline' ;" not in csp  # script-src stays self-only
        assert "frame-ancestors 'none'" in csp

        spa_res = await client.get("/some/spa/route")
        assert spa_res.headers["content-security-policy"] == csp

    async def test_docs_paths_get_relaxed_csp(self, client):
        res = await client.get("/docs")
        assert res.status_code == 200
        csp = res.headers["content-security-policy"]
        assert "https://cdn.jsdelivr.net" in csp
        assert "'unsafe-inline'" in csp

        schema = await client.get("/openapi.json")
        assert "https://cdn.jsdelivr.net" in schema.headers["content-security-policy"]

        redoc = await client.get("/redoc")
        assert "https://cdn.jsdelivr.net" in redoc.headers["content-security-policy"]

    async def test_error_responses_also_carry_headers(self, client):
        res = await client.get("/api/v1/nope-missing")
        assert res.status_code == 404
        assert res.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" in res.headers
