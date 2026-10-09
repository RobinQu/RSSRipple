"""Unit tests for the submission_guard module."""

from __future__ import annotations

import pytest

from app.services.submission_guard import SubmissionGuard


@pytest.mark.asyncio
async def test_issue_and_consume():
    sg = SubmissionGuard()
    t = await sg.issue()
    assert isinstance(t, str)
    assert await sg.consume(t) is True
    # Second consume fails (already used)
    assert await sg.consume(t) is False


@pytest.mark.asyncio
async def test_consume_unknown_token_returns_false():
    sg = SubmissionGuard()
    assert await sg.consume("no-such-token") is False


@pytest.mark.asyncio
async def test_purge_expired(monkeypatch):
    sg = SubmissionGuard()
    sg.TTL_SECONDS = 1
    # Advance time between issue and consume: issue at 100, purge at 200
    calls = iter([100.0, 100.0, 200.0])
    def _t():
        try:
            return next(calls)
        except StopIteration:
            return 1_000_000.0
    monkeypatch.setattr("app.services.submission_guard.time.monotonic", _t)
    t = await sg.issue()
    # Enough time passes — token should be purged; consume returns False
    assert await sg.consume(t) is False


@pytest.mark.asyncio
async def test_retry_after_failed_submission_not_burned(monkeypatch):
    """Regression: consume() used to delete the token up front, so any
    handler-side validation failure after consumption (422/409/500) forced
    the client to re-GET a fresh form token. Now only a rapid repeat consume
    is rejected; a later retry with the same token is accepted."""
    sg = SubmissionGuard()
    now = [1000.0]
    monkeypatch.setattr("app.services.submission_guard.time.monotonic", lambda: now[0])

    t = await sg.issue()
    assert await sg.consume(t) is True
    # Rapid double-submit (double-click, retry storm): rejected.
    now[0] += 1
    assert await sg.consume(t) is False
    # Handler failed validation; the user fixes the form and resubmits well
    # after the duplicate window: the same token is accepted again.
    now[0] += sg.DUPLICATE_WINDOW_SECONDS + 1
    assert await sg.consume(t) is True
    # A rapid duplicate of the retry is rejected again.
    now[0] += 1
    assert await sg.consume(t) is False


@pytest.mark.asyncio
async def test_consumed_token_still_expires(monkeypatch):
    sg = SubmissionGuard()
    sg.TTL_SECONDS = 100
    now = [1000.0]
    monkeypatch.setattr("app.services.submission_guard.time.monotonic", lambda: now[0])
    t = await sg.issue()
    assert await sg.consume(t) is True
    now[0] += 1000  # past TTL: purged on the next call
    assert await sg.consume(t) is False
