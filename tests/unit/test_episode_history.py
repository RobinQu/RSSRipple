"""Tests for history-backed season/episode reconciliation."""

import uuid
from types import SimpleNamespace

from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.services.episode_history import (
    _choose_convention,
    apply_episode_history_reconcile,
    apply_season_history_default,
)


def _id() -> str:
    return str(uuid.uuid4())


def _resource(channel_id: str, series_id: str, guid: str, **fields) -> FileResource:
    defaults = {
        "id": _id(),
        "channel_id": channel_id,
        "guid": guid,
        "title_raw": guid,
        "torrent_url": f"magnet:?xt=urn:btih:{guid}",
        "series_id": series_id,
        "is_batch": False,
    }
    defaults.update(fields)
    return FileResource(**defaults)


def _history(group: str | None, absolute: int, season: int, episode: int, confidence: str):
    return SimpleNamespace(
        subtitle_group=group,
        absolute_episode=absolute,
        season=season,
        episode=episode,
        episode_confidence=confidence,
    )


def test_choose_convention_accepts_one_same_group_manual():
    rows = [_history("Nix-Raws", 31, 3, 7, "manual")]
    assert _choose_convention(rows, "nix-raws") == (3, 24)


def test_choose_convention_cross_group_consensus_and_conflict():
    consensus = [
        _history("A", 30, 3, 6, "reconciled"),
        _history("B", 31, 3, 7, "reconciled"),
    ]
    assert _choose_convention(consensus, "c") == (3, 24)
    consensus[1].episode = 8
    assert _choose_convention(consensus, "c") is None


async def test_adjacent_same_group_manual_history_reconciles(db_session, sample_channel):
    series = TVSeries(id=_id(), title_cn="百女友", seasons=[
        {"season_number": 1, "episode_count": 12},
        {"season_number": 2, "episode_count": 12},
        {"season_number": 3, "episode_count": 7},
    ])
    history = _resource(
        sample_channel.id, series.id, "history-31", subtitle_group="Nix-Raws",
        season=3, episode=7, absolute_episode=31, episode_confidence="manual",
    )
    target = _resource(
        sample_channel.id, series.id, "target-32", subtitle_group="nix-raws",
        season=1, episode=32, episode_confidence="ambiguous",
    )
    db_session.add_all([series, history, target])
    await db_session.commit()

    changed = await apply_episode_history_reconcile(
        db_session, target, seasons_map={1: 12, 2: 12, 3: 7}
    )

    assert changed is True
    assert (target.season, target.episode, target.absolute_episode) == (3, 8, 32)
    assert target.episode_confidence == "reconciled"


async def test_same_episode_manual_variant_defaults_missing_season(
    db_session, sample_channel
):
    series = TVSeries(id=_id(), title_cn="染谷同学")
    manual = _resource(
        sample_channel.id, series.id, "e07-bilingual",
        subtitle_group="桜都字幕组", season=1, episode=7,
        episode_confidence="manual",
    )
    target = _resource(
        sample_channel.id, series.id, "e07-simple",
        subtitle_group="桜都字幕组", season=None, episode=7,
        episode_confidence="ambiguous",
    )
    db_session.add_all([series, manual, target])
    await db_session.commit()

    assert await apply_season_history_default(db_session, target) is True
    assert target.season == 1
    assert target.episode_confidence == "reconciled"


async def test_conflicting_same_episode_seasons_do_not_default(
    db_session, sample_channel
):
    series = TVSeries(id=_id(), title_cn="同名长篇")
    target = _resource(
        sample_channel.id, series.id, "target-e07",
        subtitle_group="Group", season=None, episode=7,
        episode_confidence="ambiguous",
    )
    rows = [
        _resource(
            sample_channel.id, series.id, "s1e07", subtitle_group="Group",
            season=1, episode=7, episode_confidence="raw",
        ),
        _resource(
            sample_channel.id, series.id, "s2e07", subtitle_group="Group",
            season=2, episode=7, episode_confidence="raw",
        ),
    ]
    db_session.add_all([series, target, *rows])
    await db_session.commit()

    assert await apply_season_history_default(db_session, target) is False
    assert target.season is None
    assert target.episode_confidence == "ambiguous"


async def test_cross_group_fallback_requires_consensus(db_session, sample_channel):
    series = TVSeries(id=_id(), title_cn="Series")
    history_a = _resource(
        sample_channel.id, series.id, "a-30", subtitle_group="Group A",
        season=3, episode=6, absolute_episode=30, episode_confidence="reconciled",
    )
    history_b = _resource(
        sample_channel.id, series.id, "b-31", subtitle_group="Group B",
        season=3, episode=7, absolute_episode=31, episode_confidence="reconciled",
    )
    target = _resource(
        sample_channel.id, series.id, "c-32", subtitle_group="Group C",
        season=1, episode=32, episode_confidence="ambiguous",
    )
    db_session.add_all([series, history_a, history_b, target])
    await db_session.commit()

    assert await apply_episode_history_reconcile(db_session, target) is True
    assert (target.season, target.episode, target.absolute_episode) == (3, 8, 32)


async def test_conflicting_history_and_manual_target_are_untouched(db_session, sample_channel):
    series = TVSeries(id=_id(), title_cn="Series")
    rows = [
        _resource(
            sample_channel.id, series.id, "a-30", subtitle_group="Group A",
            season=3, episode=6, absolute_episode=30,
            episode_confidence="reconciled",
        ),
        _resource(
            sample_channel.id, series.id, "b-31", subtitle_group="Group B",
            season=2, episode=9, absolute_episode=31,
            episode_confidence="reconciled",
        ),
    ]
    ambiguous = _resource(
        sample_channel.id, series.id, "target", subtitle_group="Group C",
        season=1, episode=32, episode_confidence="ambiguous",
    )
    manual = _resource(
        sample_channel.id, series.id, "manual", subtitle_group="Group A",
        season=1, episode=32, episode_confidence="manual",
    )
    db_session.add_all([series, *rows, ambiguous, manual])
    await db_session.commit()

    assert await apply_episode_history_reconcile(db_session, ambiguous) is False
    assert (ambiguous.season, ambiguous.episode) == (1, 32)
    assert await apply_episode_history_reconcile(db_session, manual) is False
    assert (manual.season, manual.episode) == (1, 32)
