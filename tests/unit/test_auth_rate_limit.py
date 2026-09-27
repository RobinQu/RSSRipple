"""Real database reservations; peer addresses and clocks are synthetic."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app import database
from app.models.auth_rate_limit import AuthRateLimitBucket
from app.services import auth_rate_limit as limiter


@pytest.mark.parametrize("count", [12, 40])
async def test_concurrent_sessions_cannot_exceed_peer_budget(db_engine, monkeypatch, count):
    monkeypatch.setattr(limiter, "utcnow", lambda: datetime(2026, 9, 27, 12))
    results = await asyncio.gather(*(limiter.reserve_otp_attempt("192.0.2.1") for _ in range(count)))
    assert results.count(0) == 5
    assert all(result > 0 for result in results if result != 0)
    async with database.async_session_factory() as db:
        rows = (await db.execute(select(AuthRateLimitBucket))).scalars().all()
        assert sorted(row.attempts for row in rows) == [5, min(count, limiter.GLOBAL_LIMIT)]


async def test_rotating_peers_hit_global_budget_and_expired_rows_are_removed(db_engine, monkeypatch):
    now = datetime(2026, 9, 23, 12)
    monkeypatch.setattr(limiter, "utcnow", lambda: now)
    results = [await limiter.reserve_otp_attempt(f"192.0.2.{i}") for i in range(40)]
    assert results == [0] * 30 + [60] * 10
    async with database.async_session_factory() as db:
        rows = (await db.execute(select(AuthRateLimitBucket))).scalars().all()
        assert len(rows) == 31
        assert all("192.0.2" not in row.bucket_key for row in rows)
    now += timedelta(seconds=60)
    assert await limiter.reserve_otp_attempt("192.0.2.100") == 0
    async with database.async_session_factory() as db:
        rows = (await db.execute(select(AuthRateLimitBucket))).scalars().all()
        assert len(rows) == 2
        assert all(row.attempts == 1 for row in rows)


async def test_unavailable_database_cannot_allow_reservation(monkeypatch):
    monkeypatch.setattr(database, "async_session_factory", Mock(side_effect=RuntimeError("test database down")))
    with pytest.raises(RuntimeError, match="test database down"):
        await limiter.reserve_otp_attempt("192.0.2.1")


async def test_existing_database_addition_is_idempotent_and_preserves_settings(db_engine):
    from app.models.app_setting import AppSetting

    async with database.async_session_factory() as db, db.begin():
        db.add(AppSetting(key="auth-limit-test", value="preserved"))
    async with db_engine.begin() as connection:
        await connection.run_sync(AuthRateLimitBucket.__table__.drop)
        await connection.run_sync(database.Base.metadata.create_all)
        await connection.run_sync(database.Base.metadata.create_all)
    assert await limiter.reserve_otp_attempt("192.0.2.1") == 0
    async with database.async_session_factory() as db:
        assert (await db.get(AppSetting, "auth-limit-test")).value == "preserved"


async def test_new_connections_preserve_exhausted_budget(db_engine, monkeypatch):
    monkeypatch.setattr(limiter, "utcnow", lambda: datetime(2026, 9, 23, 12))
    for _ in range(5):
        assert await limiter.reserve_otp_attempt("192.0.2.1") == 0
    await db_engine.dispose()
    assert await limiter.reserve_otp_attempt("192.0.2.1") == 60


async def test_cleanup_is_bounded_and_keeps_active_peer(db_engine, monkeypatch):
    now = datetime(2026, 9, 23, 12)
    monkeypatch.setattr(limiter, "utcnow", lambda: now)
    async with database.async_session_factory() as db, db.begin():
        db.add_all([
            AuthRateLimitBucket(bucket_key=f"peer:expired-{index}", attempts=1,
                                resets_at=now - timedelta(seconds=1))
            for index in range(105)
        ])
        db.add(AuthRateLimitBucket(bucket_key="peer:active", attempts=5,
                                  resets_at=now + timedelta(seconds=30)))
    assert await limiter.reserve_otp_attempt("192.0.2.1") == 0
    async with database.async_session_factory() as db:
        rows = (await db.execute(select(AuthRateLimitBucket))).scalars().all()
        assert len(rows) == 8
        assert sum(row.resets_at <= now for row in rows) == 5
        assert next(row.attempts for row in rows if row.bucket_key == "peer:active") == 5
