"""Production ASGI stack checks; origins and bootstrap credential are synthetic.

No browser SameSite or CSRF conclusion follows from manually setting Cookie.
"""

import httpx
import pytest

from app.config import settings
from app.main import app


@pytest.fixture
async def cors_client(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "synthetic-cors-probe")
    monkeypatch.setattr(settings, "cors_allowed_origins", ["https://ui.example.test"])
    app.middleware_stack = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.example.test") as client:
        yield client
    app.middleware_stack = None


async def test_allowed_preflight_reaches_cors_before_auth(cors_client):
    response = await cors_client.options("/api/v1/channels", headers={
        "Origin": "https://ui.example.test", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://ui.example.test"


async def test_allowed_unauthorized_response_exposes_cors(cors_client):
    response = await cors_client.get("/api/v1/channels", headers={"Origin": "https://ui.example.test"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.headers.get("access-control-allow-origin") == "https://ui.example.test"


async def test_unlisted_origin_does_not_receive_credentialed_read_permission(cors_client):
    response = await cors_client.get("/api/v1/synthetic-missing", headers={
        "Origin": "https://unlisted.example.test", "X-API-Key": "synthetic-cors-probe",
        "Cookie": "synthetic=present",
    })
    # The production SPA catch-all returns 200 for this synthetic path.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("status", [422, 404, 500])
async def test_error_responses_keep_cors(cors_client, status):
    from fastapi import HTTPException
    from fastapi.routing import APIRoute

    async def failing_endpoint():
        if status == 500:
            raise RuntimeError("Synthetic CORS error")
        raise HTTPException(status_code=status, detail="Synthetic CORS error")

    route = APIRoute("/api/v1/cors-error-probe", failing_endpoint, methods=["GET"])
    app.router.routes.insert(0, route)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://api.example.test",
        ) as client:
            response = await client.get(route.path, headers={
                "Origin": "https://ui.example.test", "X-API-Key": "synthetic-cors-probe",
            })
        assert response.status_code == status
        assert response.headers["access-control-allow-origin"] == "https://ui.example.test"
        assert response.headers["access-control-allow-credentials"] == "true"
        assert response.json()["success"] is False
    finally:
        app.router.routes.remove(route)


async def test_unlisted_preflight_is_denied(cors_client):
    response = await cors_client.options("/api/v1/channels", headers={
        "Origin": "https://unlisted.example.test", "Access-Control-Request-Method": "POST",
    })
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def test_no_origin_preserves_programmatic_auth(cors_client):
    response = await cors_client.get("/api/v1/synthetic-missing", headers={"X-API-Key": "synthetic-cors-probe"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    response = await cors_client.get("/api/v1/channels")
    assert response.status_code == 401


async def test_default_policy_exposes_no_cross_origin_response(cors_client, monkeypatch):
    monkeypatch.setattr(settings, "cors_allowed_origins", [])
    app.middleware_stack = None
    response = await cors_client.get("/api/v1/channels", headers={"Origin": "https://ui.example.test"})
    assert response.status_code == 401
    assert "access-control-allow-origin" not in response.headers


async def test_streaming_response_keeps_events_and_cors(cors_client):
    from fastapi.routing import APIRoute
    from starlette.responses import StreamingResponse

    async def events():
        yield b"event: test\ndata: synthetic\n\n"
        yield b"event: done\ndata: true\n\n"

    async def streaming_endpoint():
        return StreamingResponse(events(), media_type="text/event-stream")

    route = APIRoute("/api/v1/cors-stream-probe", streaming_endpoint, methods=["GET"])
    app.router.routes.insert(0, route)
    try:
        response = await cors_client.get(route.path, headers={
            "Origin": "https://ui.example.test", "X-API-Key": "synthetic-cors-probe",
        })
        assert response.status_code == 200
        assert response.content == b"event: test\ndata: synthetic\n\nevent: done\ndata: true\n\n"
        assert response.headers["access-control-allow-origin"] == "https://ui.example.test"
    finally:
        app.router.routes.remove(route)


@pytest.mark.parametrize("headers", [
    {"Origin": "https://unlisted.example.test"}, {"Origin": "null"},
    {"Origin": "https://ui.example.test/path"},
    {"Referer": "https://unlisted.example.test/form"},
    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
    {"Origin": "https://unlisted.example.test", "X-API-Key": "synthetic-cors-probe"},
])
async def test_untrusted_logout_has_no_cookie_side_effect(cors_client, headers):
    response = await cors_client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("headers", [
    {"Origin": "http://api.example.test"}, {"Origin": "http://api.example.test:80"},
    {"Origin": "https://ui.example.test"}, {"Referer": "http://api.example.test/form"},
    {"Sec-Fetch-Site": "same-origin"}, {},
])
async def test_trusted_or_programmatic_logout_remains_supported(cors_client, headers):
    response = await cors_client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 200
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_untrusted_origin_cannot_reach_business_mutation(cors_client):
    response = await cors_client.post("/api/v1/channels", headers={
        "Origin": "https://unlisted.example.test", "X-API-Key": "synthetic-cors-probe",
    }, json={})
    assert response.status_code == 403
    allowed = await cors_client.post("/api/v1/channels", headers={
        "Origin": "https://ui.example.test", "X-API-Key": "synthetic-cors-probe",
    }, json={})
    assert allowed.status_code == 422
    assert allowed.headers["access-control-allow-origin"] == "https://ui.example.test"


@pytest.mark.parametrize("headers", [
    [("Origin", "http://api.example.test"), ("Origin", "https://unlisted.example.test")],
    [("Origin", "http://api.example.test"), ("Origin", "http://api.example.test")],
    [("Referer", "http://api.example.test/form"), ("Referer", "https://unlisted.example.test")],
])
async def test_duplicate_provenance_fails_closed(cors_client, headers):
    response = await cors_client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


async def test_untrusted_forwarded_headers_do_not_change_origin(cors_client):
    response = await cors_client.post("/api/v1/auth/logout", headers={
        "Origin": "https://unlisted.example.test",
        "Forwarded": "proto=https;host=unlisted.example.test",
        "X-Forwarded-Host": "unlisted.example.test", "X-Forwarded-Proto": "https",
    })
    assert response.status_code == 403


async def test_external_https_scope_preserves_same_origin(cors_client):
    # Models ASGI scope after trusted proxy processing, not arbitrary headers.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://public.example.test") as client:
        response = await client.post("/api/v1/auth/logout", headers={"Origin": "https://public.example.test"})
    assert response.status_code == 200


async def test_gzip_and_vary_keep_origin(cors_client):
    from fastapi.routing import APIRoute
    from starlette.responses import Response

    async def large_response():
        return Response(b"synthetic " * 500, media_type="text/plain")

    route = APIRoute("/api/v1/cors-large-probe", large_response, methods=["GET"])
    app.router.routes.insert(0, route)
    try:
        response = await cors_client.get(route.path, headers={
            "Origin": "https://ui.example.test", "X-API-Key": "synthetic-cors-probe",
            "Accept-Encoding": "gzip",
        })
        assert response.content == b"synthetic " * 500
        assert response.headers["content-encoding"] == "gzip"
        assert {v.strip().lower() for v in response.headers["vary"].split(",")} >= {"origin", "accept-encoding"}
    finally:
        app.router.routes.remove(route)


async def test_poster_mount_still_requires_auth_and_keeps_cors(cors_client, tmp_path, monkeypatch):
    poster_mount = next(route for route in app.routes if getattr(route, "name", None) == "poster-cache")
    monkeypatch.setattr(poster_mount.app, "all_directories", [str(tmp_path)])
    payload = b"synthetic image bytes; no real user data"
    (tmp_path / "synthetic.png").write_bytes(payload)
    response = await cors_client.get("/posters/synthetic.png", headers={"Origin": "https://ui.example.test"})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "https://ui.example.test"
    response = await cors_client.get("/posters/synthetic.png", headers={
        "Origin": "https://ui.example.test", "X-API-Key": "synthetic-cors-probe",
    })
    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["access-control-allow-origin"] == "https://ui.example.test"


@pytest.mark.parametrize("origin", ["https://[broken", "http://api.example.test:bad-port"])
async def test_malformed_origin_is_forbidden_instead_of_server_error(cors_client, origin):
    response = await cors_client.post("/api/v1/auth/logout", headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert "set-cookie" not in response.headers
