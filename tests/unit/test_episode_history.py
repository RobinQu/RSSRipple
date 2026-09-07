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


# ---------------------------------------------------------------------------
# Far extrapolation (manual-anchored) + sibling heal
# ---------------------------------------------------------------------------


def test_choose_convention_require_manual_anchor():
    rows = [
        _history("A", 30, 3, 6, "reconciled"),
        _history("B", 31, 3, 7, "reconciled"),
    ]
    # Consensus alone is enough adjacency-locally, but not for far jumps.
    assert _choose_convention(rows, "c") == (3, 24)
    assert _choose_convention(rows, "c", require_manual=True) is None
    rows.append(_history("C", 29, 3, 5, "manual"))
    assert _choose_convention(rows, "c", require_manual=True) == (3, 24)


async def test_far_extrapolation_with_manual_anchor(db_session, sample_channel):
    """A one-off manual fix generalizes across a release gap: absolute 36
    arrives weeks after the abs-30 correction, no adjacent history exists."""
    series = TVSeries(id=_id(), title_cn="百女友")
    anchor = _resource(
        sample_channel.id, series.id, "anchor-30", subtitle_group="Nix-Raws",
        season=3, episode=6, absolute_episode=30, episode_confidence="manual",
    )
    target = _resource(
        sample_channel.id, series.id, "target-36", subtitle_group="LoliHouse",
        season=1, episode=36, episode_confidence="ambiguous",
    )
    db_session.add_all([series, anchor, target])
    await db_session.commit()

    changed = await apply_episode_history_reconcile(
        db_session, target, seasons_map={1: 12, 2: 12, 3: 12}
    )

    assert changed is True
    assert (target.season, target.episode, target.absolute_episode) == (3, 12, 36)
    assert target.episode_confidence == "reconciled"


async def test_far_extrapolation_requires_manual_anchor(db_session, sample_channel):
    series = TVSeries(id=_id(), title_cn="百女友")
    auto = _resource(
        sample_channel.id, series.id, "auto-30", subtitle_group="Nix-Raws",
        season=3, episode=6, absolute_episode=30, episode_confidence="reconciled",
    )
    target = _resource(
        sample_channel.id, series.id, "target-36", subtitle_group="Nix-Raws",
        season=1, episode=36, episode_confidence="ambiguous",
    )
    db_session.add_all([series, auto, target])
    await db_session.commit()

    changed = await apply_episode_history_reconcile(
        db_session, target, seasons_map={1: 12, 2: 12, 3: 12}
    )

    assert changed is False
    assert (target.season, target.episode) == (1, 36)


async def test_far_extrapolation_respects_season_count(db_session, sample_channel):
    """The convention's implied episode may not overshoot the known season
    count (+ tolerance) even with a manual anchor."""
    series = TVSeries(id=_id(), title_cn="百女友")
    anchor = _resource(
        sample_channel.id, series.id, "anchor-30", subtitle_group="Nix-Raws",
        season=3, episode=6, absolute_episode=30, episode_confidence="manual",
    )
    target = _resource(
        sample_channel.id, series.id, "target-52", subtitle_group="Nix-Raws",
        season=1, episode=52, episode_confidence="ambiguous",
    )
    db_session.add_all([series, anchor, target])
    await db_session.commit()

    changed = await apply_episode_history_reconcile(
        db_session, target, seasons_map={1: 12, 2: 12, 3: 12}
    )

    assert changed is False


async def test_heal_sibling_episodes(db_session, sample_channel):
    from app.services.episode_history import heal_sibling_episodes

    series = TVSeries(id=_id(), title_cn="百女友")
    anchor = _resource(
        sample_channel.id, series.id, "anchor-30", subtitle_group="Nix-Raws",
        season=3, episode=6, absolute_episode=30, episode_confidence="manual",
    )
    ambiguous_far = _resource(
        sample_channel.id, series.id, "amb-36", subtitle_group="LoliHouse",
        season=1, episode=36, episode_confidence="ambiguous",
    )
    ambiguous_adjacent = _resource(
        sample_channel.id, series.id, "amb-31", subtitle_group="Nix-Raws",
        season=1, episode=31, episode_confidence="ambiguous",
    )
    manual_sibling = _resource(
        sample_channel.id, series.id, "manual-29", subtitle_group="Other",
        season=3, episode=5, absolute_episode=29, episode_confidence="manual",
    )
    db_session.add_all(
        [series, anchor, ambiguous_far, ambiguous_adjacent, manual_sibling]
    )
    await db_session.commit()

    healed = await heal_sibling_episodes(
        db_session, anchor, seasons_map={1: 12, 2: 12, 3: 12}
    )

    assert set(healed) == {ambiguous_far.id, ambiguous_adjacent.id}
    assert (ambiguous_far.season, ambiguous_far.episode) == (3, 12)
    assert (ambiguous_adjacent.season, ambiguous_adjacent.episode) == (3, 7)
    assert ambiguous_far.episode_confidence == "reconciled"
    # Manual rows are never touched.
    assert (manual_sibling.season, manual_sibling.episode) == (3, 5)


async def test_heal_sibling_episodes_scopes_to_work_and_channel(
    db_session, sample_channel
):
    from app.models.channel import Channel
    from app.services.episode_history import heal_sibling_episodes

    series = TVSeries(id=_id(), title_cn="百女友")
    other_series = TVSeries(id=_id(), title_cn="另一部")
    channel_b = Channel(
        id=_id(), name="Channel B", type="rss_feed",
        url="https://example.com/rss-b", fetch_interval=1800, status="active",
        field_mapping={"list_locator": {"source": "entries"},
                       "field_mappings": {"torrent_url": {"source": "link"}}},
        metadata_agent_enabled=False,
    )
    anchor = _resource(
        sample_channel.id, series.id, "anchor-30", subtitle_group="Nix-Raws",
        season=3, episode=6, absolute_episode=30, episode_confidence="manual",
    )
    other_work = _resource(
        sample_channel.id, other_series.id, "other-work", subtitle_group="Nix-Raws",
        season=1, episode=36, episode_confidence="ambiguous",
    )
    other_channel = _resource(
        channel_b.id, series.id, "other-channel", subtitle_group="Nix-Raws",
        season=1, episode=36, episode_confidence="ambiguous",
    )
    db_session.add_all(
        [series, other_series, channel_b, anchor, other_work, other_channel]
    )
    await db_session.commit()

    healed = await heal_sibling_episodes(
        db_session, anchor, seasons_map={1: 12, 2: 12, 3: 12}
    )

    assert healed == []
    assert other_work.episode_confidence == "ambiguous"
    assert other_channel.episode_confidence == "ambiguous"
