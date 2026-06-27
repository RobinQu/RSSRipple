"""Unit tests for the submission_guard module."""

from __future__ import annotations

import asyncio

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
