"""Unit tests for scripts/reconcile_season_backfill.py (Case-1 stock backfill).

Covers the candidate selection (SQL against a test DB) and the per-resource
decision logic ``reconcile_stock_resource`` (pure, driven with ORM rows /
namespace doubles).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.file_resource import FileResource
from app.models.series import TVSeries
from scripts.reconcile_season_backfill import (
    OUTCOME_MARKED_AMBIGUOUS,
    OUTCOME_SEASON_DEFAULTED,
    OUTCOME_SEASON_DERIVED,
    OUTCOME_SKIPPED,
    OUTCOME_UNCHANGED,
    candidate_query,
    reconcile_stock_resource,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _resource(channel_id, **overrides):
    base = dict(
        id=_uuid(), channel_id=channel_id, guid=_uuid(),
        title_raw="[G] Title - 01 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
        episode=1, parsed_at=datetime.now(UTC),
    )
    base.update(overrides)
    return FileResource(**base)


async def test_candidate_query_selection(db_session, sample_channel):
    """Only series-linked, non-batch, non-manual/non-ambiguous rows with a
    missing season (or a season+absolute pair) are selected."""
    series = TVSeries(id=_uuid(), title_cn="剧", content_type="tv")
    db_session.add(series)
    ch = sample_channel.id
    rows = {
        "season_none": _resource(ch, series_id=series.id, season=None, episode=3),
        "season_abs": _resource(ch, series_id=series.id, season=2, episode=5,
                                absolute_episode=30, episode_confidence="reconciled"),
        # excluded: manual
        "manual": _resource(ch, series_id=series.id, season=None, episode=3,
                            episode_confidence="manual"),
        # excluded: already ambiguous
        "ambiguous": _resource(ch, series_id=series.id, season=None, episode=3,
                               episode_confidence="ambiguous"),
        # excluded: batch
        "batch": _resource(ch, series_id=series.id, season=None, episode=None,
                           is_batch=True),
        # excluded: season known, no absolute number
        "plain": _resource(ch, series_id=series.id, season=1, episode=3),
        # excluded: not series-linked
        "unlinked": _resource(ch, season=None, episode=3),
    }
    db_session.add_all(rows.values())
    await db_session.flush()

    selected = (await db_session.execute(candidate_query())).scalars().all()
    selected_ids = {r.id for r in selected}
    assert selected_ids == {rows["season_none"].id, rows["season_abs"].id}


def _series_row(**kw):
    base = dict(number_of_seasons=None, seasons=None)
    base.update(kw)
    return TVSeries(id=_uuid(), title_cn="剧", content_type="tv", **base)


def test_reconcile_stock_resource_derives_season_from_absolute():
    r = _resource("ch", season=None, episode=89, absolute_episode=89,
                  episode_confidence="reconciled")
    series = _series_row(
        number_of_seasons=4,
        seasons=[{"season_number": n, "episode_count": 24} for n in (1, 2, 3, 4)],
    )
    assert reconcile_stock_resource(r, series) == OUTCOME_SEASON_DERIVED
    assert (r.season, r.episode) == (4, 17)
    assert r.episode_confidence == "reconciled"


def test_reconcile_stock_resource_defaults_single_season():
    r = _resource("ch", season=None, episode=3)
    series = _series_row(number_of_seasons=1,
                         seasons=[{"season_number": 1, "episode_count": 12}])
    assert reconcile_stock_resource(r, series) == OUTCOME_SEASON_DEFAULTED
    assert r.season == 1


def test_reconcile_stock_resource_marks_ambiguous_multi_season():
    r = _resource("ch", season=None, episode=3)
    series = _series_row(
        number_of_seasons=3,
        seasons=[{"season_number": n, "episode_count": 24} for n in (1, 2, 3)],
    )
    assert reconcile_stock_resource(r, series) == OUTCOME_MARKED_AMBIGUOUS
    assert r.season is None
    assert r.episode_confidence == "ambiguous"


def test_reconcile_stock_resource_marks_ambiguous_on_conflict():
    """Season marker contradicts the absolute arithmetic -> ambiguous."""
    r = _resource("ch", season=1, episode=89, absolute_episode=89,
                  episode_confidence="raw")
    series = _series_row(
        seasons=[{"season_number": n, "episode_count": 24} for n in (1, 2, 3, 4)],
    )
    assert reconcile_stock_resource(r, series) == OUTCOME_MARKED_AMBIGUOUS
    assert r.episode_confidence == "ambiguous"


def test_reconcile_stock_resource_skipped_without_evidence():
    r = _resource("ch", season=None, episode=3)
    assert reconcile_stock_resource(r, None) == OUTCOME_SKIPPED
    assert reconcile_stock_resource(r, _series_row()) == OUTCOME_SKIPPED
    assert r.season is None
    assert r.episode_confidence is None


def test_reconcile_stock_resource_unchanged_when_consistent():
    r = _resource("ch", season=4, episode=17, absolute_episode=89,
                  episode_confidence="reconciled")
    series = _series_row(
        seasons=[{"season_number": n, "episode_count": 24} for n in (1, 2, 3, 4)],
    )
    assert reconcile_stock_resource(r, series) == OUTCOME_UNCHANGED
    assert (r.season, r.episode) == (4, 17)
    assert r.episode_confidence == "reconciled"


def test_reconcile_stock_resource_never_touches_manual():
    """Selection excludes manual rows, and the decision logic is a no-op for
    one that slips through."""
    r = _resource("ch", season=None, episode=3, episode_confidence="manual")
    series = _series_row(number_of_seasons=1,
                         seasons=[{"season_number": 1, "episode_count": 12}])
    assert reconcile_stock_resource(r, series) == OUTCOME_UNCHANGED
    assert r.season is None
    assert r.episode_confidence == "manual"


async def test_candidate_query_empty_db(db_session):
    assert (await db_session.execute(select(FileResource))).scalars().all() == []
    assert (await db_session.execute(candidate_query())).scalars().all() == []
