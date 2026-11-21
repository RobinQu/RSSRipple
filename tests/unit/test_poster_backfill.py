"""Tests for generic remote-poster backfill."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.audio_work import AudioWork
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import poster_backfill as pb


def _uuid() -> str:
    return str(uuid.uuid4())


def _series(poster: str | None, *, manual: bool = False) -> TVSeries:
    return TVSeries(
        id=_uuid(), title_cn="剧", content_type="tv", season_number=1,
        poster_url=poster,
        manually_edited_fields=["poster_url"] if manual else None,
    )


async def test_dry_run_counts_without_downloading(db_session):
    db_session.add_all([
        _series("https://x/a.jpg"),
        _series("/posters/local.jpg"),
        _series("https://x/manual.jpg", manual=True),
    ])
    await db_session.flush()
    download = AsyncMock(return_value="/posters/a.jpg")
    with patch("app.services.poster_backfill.download_and_cache_poster", download):
        report = await pb.cache_remote_posters(
            db_session, models=["tv"], apply=False,
        )

    assert report["tv"] == {
        "scanned": 2, "eligible": 1, "cached": 0, "failed": 0, "skipped": 1,
    }
    download.assert_not_awaited()


async def test_apply_caches_success_and_keeps_failures_remote(db_session):
    ok = _series("https://x/ok.jpg")
    failed = _series("https://x/fail.jpg")
    db_session.add_all([ok, failed])
    await db_session.flush()

    async def _download(url):
        return "/posters/ok.jpg" if url.endswith("/ok.jpg") else None

    with patch(
        "app.services.poster_backfill.download_and_cache_poster",
        AsyncMock(side_effect=_download),
    ):
        report = await pb.cache_remote_posters(
            db_session, models=["tv"], apply=True,
        )

    assert report["tv"]["cached"] == 1
    assert report["tv"]["failed"] == 1
    assert ok.poster_url == "/posters/ok.jpg"
    assert failed.poster_url == "https://x/fail.jpg"


async def test_limit_and_model_selection(db_session):
    db_session.add_all([
        _series("https://x/1.jpg"),
        _series("https://x/2.jpg"),
        Movie(
            id=_uuid(), title_cn="电影", content_type="movie",
            poster_url="https://x/m.jpg",
        ),
        AudioWork(
            id=_uuid(), title_cn="音频", content_type="asmr",
            poster_url="https://x/a.jpg",
        ),
        WorkCollection(
            id=_uuid(), title_cn="合集", external_source="series_group",
            poster_url="https://x/c.jpg",
        ),
    ])
    await db_session.flush()

    download = AsyncMock(side_effect=lambda url: f"/posters/{url.rsplit('/', 1)[-1]}")
    with patch("app.services.poster_backfill.download_and_cache_poster", download):
        report = await pb.cache_remote_posters(
            db_session, models=["tv", "movie"], limit_per_model=1, apply=True,
        )

    assert set(report) == {"tv", "movie"}
    assert report["tv"]["scanned"] == 1
    assert report["movie"]["scanned"] == 1
    assert download.await_count == 2


async def test_unknown_model_rejected(db_session):
    with pytest.raises(ValueError):
        await pb.cache_remote_posters(db_session, models=["bogus"])
