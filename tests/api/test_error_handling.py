"""Tests for global exception handlers: structured JSON errors, logging, dev mode."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app as fastapi_app
from app.database import get_db

# ---------------------------------------------------------------------------
# Fixture: client that does NOT re-raise app-level exceptions.
# Needed for 500-path tests because Starlette's ServerErrorMiddleware re-raises
# after calling the error handler, and ASGITransport raises that by default.
# ---------------------------------------------------------------------------

MOCK_VALIDATE = "app.api.v1.channels.validate_rss_url"


@pytest_asyncio.fixture
async def error_client(db_session_factory, monkeypatch):
    """Async HTTP client that captures 500 responses instead of raising."""
    # No-op task queue
    from app.services import task_queue as tq_mod
    fake_queue = AsyncMock()
    fake_queue.enqueue = AsyncMock(return_value={"job_id": "j", "status": "queued"})
    fake_queue.status = AsyncMock(return_value={"status": "done"})
    fake_queue.start = AsyncMock()
    fake_queue.stop = AsyncMock()
    monkeypatch.setattr(tq_mod, "task_queue", fake_queue)

    from app.services import submission_guard as sg_mod
    class _FakeGuard:
        async def issue(self) -> str:
            return "test-token"
        async def consume(self, token: str) -> bool:
            return True
    monkeypatch.setattr(sg_mod, "submission_guard", _FakeGuard())

    import app.services.scheduler as sch_mod
    monkeypatch.setattr(sch_mod, "reschedule_channel", lambda ch: None)
    monkeypatch.setattr(sch_mod, "unschedule_channel", lambda cid: None)

    from tests.api.conftest import _build_test_app
    test_app = _build_test_app(db_session_factory)
    transport = ASGITransport(app=test_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_validation_error_returns_structured_json(client):
    """RequestValidationError returns {"success": false, "error": {"code": "VALIDATION_ERROR"}}."""
    res = await client.post("/api/v1/channels", json={"name": "x", "url": True})
    assert res.status_code == 422
    body = res.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "message" in body["error"]


@pytest.mark.asyncio
async def test_missing_required_field_returns_structured_json(client):
    """Missing required fields return the same structured format."""
    res = await client.post("/api/v1/channels", json={})
    assert res.status_code == 422
    body = res.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    # Ensure the FastAPI default {"detail": [...]} format is NOT returned
    assert "detail" not in body


@pytest.mark.asyncio
async def test_validation_error_is_logged_as_warning(client, caplog):
    """Validation errors are logged at WARNING level."""
    with caplog.at_level(logging.WARNING, logger="app.main"):
        await client.post("/api/v1/channels", json={"name": "x", "url": True})
    assert any("Validation error" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 405 Method Not Allowed / 404 from Starlette → structured JSON
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_method_not_allowed_returns_structured_json(client):
    """405 HTTPException returns structured JSON."""
    res = await client.delete("/api/v1/channels")
    assert res.status_code == 405
    body = res.json()
    assert body["success"] is False
    assert body["error"]["code"] == "405"
    assert "detail" not in body


# ---------------------------------------------------------------------------
# 500 Unhandled exception → structured JSON + error log
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unhandled_exception_returns_500_json(error_client, caplog):
    """An unhandled exception in a route returns structured 500 JSON."""
    with patch(MOCK_VALIDATE, new_callable=AsyncMock, side_effect=RuntimeError("DB exploded")):
        with caplog.at_level(logging.ERROR, logger="app.main"):
            res = await error_client.post(
                "/api/v1/channels",
                json={"name": "x", "url": "http://test.example/rss.xml"},
            )
    assert res.status_code == 500
    body = res.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert "detail" not in body


@pytest.mark.asyncio
async def test_unhandled_exception_logs_at_error_level(error_client, caplog):
    """Unhandled exceptions are logged at ERROR level with traceback."""
    with patch(MOCK_VALIDATE, new_callable=AsyncMock, side_effect=RuntimeError("kaboom")):
        with caplog.at_level(logging.ERROR, logger="app.main"):
            await error_client.post(
                "/api/v1/channels",
                json={"name": "x", "url": "http://test.example/rss.xml"},
            )
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(error_records) >= 1
    assert any("kaboom" in r.message or "kaboom" in str(r.exc_info) for r in error_records)


# ---------------------------------------------------------------------------
# dev_mode: stack trace included in 500 response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dev_mode_includes_stack_trace_in_500(error_client):
    """When dev_mode=True, 500 response body contains error detail with stack trace."""
    with patch("app.main.settings.dev_mode", True):
        with patch(MOCK_VALIDATE, new_callable=AsyncMock, side_effect=ValueError("test error")):
            res = await error_client.post(
                "/api/v1/channels",
                json={"name": "x", "url": "http://test.example/rss.xml"},
            )
    assert res.status_code == 500
    body = res.json()
    assert body["error"].get("stack") is not None, "dev_mode should include stack trace"
    assert "test error" in body["error"]["stack"]


@pytest.mark.asyncio
async def test_prod_mode_omits_stack_trace(error_client):
    """When dev_mode=False, 500 response body does NOT contain stack details."""
    with patch("app.main.settings.dev_mode", False):
        with patch(MOCK_VALIDATE, new_callable=AsyncMock, side_effect=ValueError("secret internals")):
            res = await error_client.post(
                "/api/v1/channels",
                json={"name": "x", "url": "http://test.example/rss.xml"},
            )
    assert res.status_code == 500
    body = res.json()
    assert "detail" not in body["error"]
    assert "secret internals" not in res.text


# ---------------------------------------------------------------------------
# Channel creation: specific 500 scenario (DB flush error)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_channel_create_db_error_returns_500_json(error_client):
    """If DB flush fails during channel creation, endpoint returns structured 500."""
    from sqlalchemy.exc import OperationalError

    with patch(MOCK_VALIDATE, new_callable=AsyncMock, return_value=(True, "ok", 10, 10)):
        with patch("sqlalchemy.ext.asyncio.AsyncSession.flush",
                   new_callable=AsyncMock,
                   side_effect=OperationalError("database is locked", None, None)):
            res = await error_client.post(
                "/api/v1/channels",
                json={"name": "newchan", "url": "http://test.example/rss.xml"},
            )
    assert res.status_code == 500
    body = res.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INTERNAL_SERVER_ERROR"
