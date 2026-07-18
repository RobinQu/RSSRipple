"""Tests for automatic cleanup of stale unresolved FileResources.

Covers the per-channel opt-in sweep (``cleanup_stale_unresolved_resources``)
and the single-channel entry point (``cleanup_channel_unresolved_resources``)
that backs both the daily scheduler job (``force=False``) and the manual API
trigger (``force=True``).

A resource is deleted when: it belongs to an opted-in channel (or ``force``),
has no linked work, was never matched (``metadata_matched_at IS NULL``), has
had no manual handling (``episode_confidence != 'manual'`` and no
``DownloadTask``), and ``created_at`` is older than the channel's threshold.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.services.resource_cleanup import (
    cleanup_channel_unresolved_resources,
    cleanup_stale_unresolved_resources,
)
from app.utils.time import utcnow


def _uuid() -> str:
    return str(uuid.uuid4())


def _make_resource(
    channel_id: str,
    *,
    guid: str | None = None,
    created_days_ago: float = 30,
    series_id: str | None = None,
    movie_id: str | None = None,
    audio_work_id: str | None = None,
    metadata_matched_at=None,
    episode_confidence: str | None = None,
    metadata_failure_type: str | None = None,
    metadata_attempts: int = 0,
) -> FileResource:
    """Build a persisted-ready FileResource with a backdated ``created_at``."""
    return FileResource(
        id=_uuid(),
        channel_id=channel_id,
        guid=guid or f"guid-{_uuid()}",
        title_raw="[Group] Show - 01 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
        series_id=series_id,
        movie_id=movie_id,
        audio_work_id=audio_work_id,
        metadata_matched_at=metadata_matched_at,
        episode_confidence=episode_confidence,
        metadata_failure_type=metadata_failure_type,
        metadata_attempts=metadata_attempts,
        created_at=utcnow() - timedelta(days=created_days_ago),
    )


@pytest.mark.asyncio
async def test_deletes_old_unresolved_resource(db_session, sample_channel):
    """The canonical case: old, unresolved, un-handled -> deleted."""
    res = _make_resource(sample_channel.id, created_days_ago=30)
    db_session.add(res)
    await db_session.commit()

    sample_channel.auto_cleanup_unresolved_enabled = True
    sample_channel.auto_cleanup_unresolved_days = 21
    await db_session.commit()

    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 1
    remaining = (
        await db_session.execute(
            select(FileResource).where(FileResource.id == res.id)
        )
    ).scalar_one_or_none()
    assert remaining is None


@pytest.mark.asyncio
async def test_keeps_matched_series(db_session, sample_channel, sample_series):
    """A resource linked to a series is resolved - never cleaned."""
    res = _make_resource(
        sample_channel.id, created_days_ago=30, series_id=sample_series.id
    )
    db_session.add(res)
    await db_session.commit()

    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 0
    assert (
        await db_session.execute(
            select(FileResource).where(FileResource.id == res.id)
        )
    ).scalar_one() is not None


@pytest.mark.asyncio
async def test_keeps_matched_movie(db_session, sample_channel, sample_movie):
    """Linked movie -> resolved -> kept."""
    res = _make_resource(
        sample_channel.id, created_days_ago=30, movie_id=sample_movie.id
    )
    db_session.add(res)
    await db_session.commit()

    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 0


@pytest.mark.asyncio
async def test_keeps_metadata_matched(db_session, sample_channel):
    """``metadata_matched_at`` set means it was resolved (even with no FK)."""
    res = _make_resource(
        sample_channel.id,
        created_days_ago=30,
        metadata_matched_at=utcnow() - timedelta(days=29),
    )
    db_session.add(res)
    await db_session.commit()

    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 0


@pytest.mark.asyncio
async def test_keeps_manual_episode_confidence(db_session, sample_channel):
    """``episode_confidence='manual'`` means a user edited it -> keep."""
    res = _make_resource(
        sample_channel.id, created_days_ago=30, episode_confidence="manual"
    )
    db_session.add(res)
    await db_session.commit()

    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 0
    assert (
        await db_session.execute(
            select(FileResource).where(FileResource.id == res.id)
        )
    ).scalar_one() is not None


@pytest.mark.asyncio
async def test_keeps_resource_with_download_task(
    db_session, sample_channel, sample_downloader
):
    """A resource that triggered a download was manually handled -> keep."""
    res = _make_resource(sample_channel.id, created_days_ago=30)
    db_session.add(res)
    await db_session.flush()
    db_session.add(
        DownloadTask(
            id=_uuid(),
            file_resource_id=res.id,
            downloader_id=sample_downloader.id,
            download_dir="/downloads",
            status="completed",
        )
    )
    await db_session.commit()

    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 0
    assert (
        await db_session.execute(
            select(FileResource).where(FileResource.id == res.id)
        )
    ).scalar_one() is not None


@pytest.mark.asyncio
async def test_keeps_resource_newer_than_threshold(db_session, sample_channel):
    """Within the threshold window -> kept even though unresolved."""
    res = _make_resource(sample_channel.id, created_days_ago=5)
    db_session.add(res)
    await db_session.commit()

    sample_channel.auto_cleanup_unresolved_days = 21
    await db_session.commit()

    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 0
    assert (
        await db_session.execute(
            select(FileResource).where(FileResource.id == res.id)
        )
    ).scalar_one() is not None


@pytest.mark.asyncio
async def test_cleans_non_work_resource(db_session, sample_channel):
    """``non_work`` failure type has no linked work -> cleaned (per spec)."""
    res = _make_resource(
        sample_channel.id,
        created_days_ago=30,
        metadata_failure_type="non_work",
        metadata_attempts=1,
    )
    db_session.add(res)
    await db_session.commit()

    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 1
    assert (
        await db_session.execute(
            select(FileResource).where(FileResource.id == res.id)
        )
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_skips_when_channel_disabled_and_not_forced(db_session, sample_channel):
    """Automatic path: disabled channel -> nothing deleted."""
    res = _make_resource(sample_channel.id, created_days_ago=30)
    db_session.add(res)
    await db_session.commit()

    sample_channel.auto_cleanup_unresolved_enabled = False
    sample_channel.auto_cleanup_unresolved_days = 21
    await db_session.commit()

    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=False
    )
    assert deleted == 0
    assert (
        await db_session.execute(
            select(FileResource).where(FileResource.id == res.id)
        )
    ).scalar_one() is not None


@pytest.mark.asyncio
async def test_force_runs_even_when_disabled(db_session, sample_channel):
    """Manual trigger ignores the toggle."""
    res = _make_resource(sample_channel.id, created_days_ago=30)
    db_session.add(res)
    await db_session.commit()

    sample_channel.auto_cleanup_unresolved_enabled = False
    sample_channel.auto_cleanup_unresolved_days = 21
    await db_session.commit()

    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 1


@pytest.mark.asyncio
async def test_respects_per_channel_threshold(db_session, sample_channel):
    """A 10-day-old resource is stale at 7 days but fresh at 21."""
    fresh = _make_resource(
        sample_channel.id, created_days_ago=10, guid="fresh"
    )
    stale = _make_resource(
        sample_channel.id, created_days_ago=10, guid="stale"
    )
    db_session.add_all([fresh, stale])
    await db_session.commit()

    # Threshold 7 days -> both 10-day-old resources are stale
    sample_channel.auto_cleanup_unresolved_days = 7
    await db_session.commit()
    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 2

    # Re-create at 10 days and use a 21-day threshold -> both kept
    fresh2 = _make_resource(
        sample_channel.id, created_days_ago=10, guid="fresh2"
    )
    db_session.add(fresh2)
    await db_session.commit()
    sample_channel.auto_cleanup_unresolved_days = 21
    await db_session.commit()
    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 0


@pytest.mark.asyncio
async def test_returns_zero_for_unknown_channel(db_session):
    """Unknown channel id -> 0, no error."""
    deleted = await cleanup_channel_unresolved_resources(
        db_session, "does-not-exist", force=True
    )
    assert deleted == 0


@pytest.mark.asyncio
async def test_correct_count_mixed(db_session, sample_channel, sample_series):
    """Mix of deletable and keepable resources -> only the stale ones go."""
    dele = _make_resource(sample_channel.id, created_days_ago=30, guid="del")
    kept_match = _make_resource(
        sample_channel.id,
        created_days_ago=30,
        guid="match",
        series_id=sample_series.id,
    )
    kept_manual = _make_resource(
        sample_channel.id,
        created_days_ago=30,
        guid="manual",
        episode_confidence="manual",
    )
    kept_new = _make_resource(sample_channel.id, created_days_ago=5, guid="new")
    db_session.add_all([dele, kept_match, kept_manual, kept_new])
    await db_session.commit()

    sample_channel.auto_cleanup_unresolved_days = 21
    await db_session.commit()

    deleted = await cleanup_channel_unresolved_resources(
        db_session, sample_channel.id, force=True
    )
    assert deleted == 1
    remaining_guids = {
        r.guid
        for r in (
            await db_session.execute(
                select(FileResource).where(
                    FileResource.channel_id == sample_channel.id
                )
            )
        ).scalars()
    }
    assert remaining_guids == {"match", "manual", "new"}


@pytest.mark.asyncio
async def test_sweep_only_enabled_channels(
    db_session, sample_channel, sample_series
):
    """``cleanup_stale_unresolved_resources`` skips disabled channels."""
    # sample_channel defaults to auto_cleanup disabled
    res = _make_resource(sample_channel.id, created_days_ago=30)
    db_session.add(res)
    await db_session.commit()

    report = await cleanup_stale_unresolved_resources(db_session)
    assert report == {"channels": 0, "deleted": 0}
    assert (
        await db_session.execute(
            select(FileResource).where(FileResource.id == res.id)
        )
    ).scalar_one() is not None


@pytest.mark.asyncio
async def test_sweep_deletes_on_enabled_channel(db_session, sample_channel):
    """The sweep cleans an opted-in channel and reports counts."""
    res = _make_resource(sample_channel.id, created_days_ago=30)
    db_session.add(res)
    await db_session.commit()

    sample_channel.auto_cleanup_unresolved_enabled = True
    sample_channel.auto_cleanup_unresolved_days = 21
    await db_session.commit()

    report = await cleanup_stale_unresolved_resources(db_session)
    assert report == {"channels": 1, "deleted": 1}
    assert (
        await db_session.execute(
            select(FileResource).where(FileResource.id == res.id)
        )
    ).scalar_one_or_none() is None
