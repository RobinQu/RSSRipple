"""In-process coverage for episode_history helper and caller-supplied-rows
branches.

The DB-scan paths (``history_rows=None``) are exercised elsewhere; these
tests cover the pure convention helpers (``_convention`` /
``_single_convention`` / ``_choose_convention``) and the ``history_rows``
fast path of ``apply_season_history_default`` /
``apply_episode_history_reconcile``, where the caller passes pre-fetched
sibling rows and the ``db`` argument is unused.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.episode_history import (
    _choose_convention,
    _convention,
    _single_convention,
    apply_episode_history_reconcile,
    apply_season_history_default,
)


def _row(
    group: str | None,
    absolute: int | None,
    season: int | None,
    episode: int | None,
    confidence: str = "reconciled",
    **extra,
):
    return SimpleNamespace(
        id=extra.pop("id", "row"),
        series_id=extra.pop("series_id", "s1"),
        channel_id=extra.pop("channel_id", "ch1"),
        is_batch=extra.pop("is_batch", False),
        subtitle_group=group,
        absolute_episode=absolute,
        season=season,
        episode=episode,
        episode_confidence=confidence,
        **extra,
    )


def _target(**overrides):
    defaults = dict(
        id="target",
        series_id="s1",
        channel_id="ch1",
        is_batch=False,
        subtitle_group="Group",
        subtitle_groups=None,
        season=None,
        episode=32,
        absolute_episode=None,
        episode_confidence="ambiguous",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# _convention
# ---------------------------------------------------------------------------


class TestConvention:
    def test_missing_components_yield_none(self):
        assert _convention(_row("g", None, 3, 7)) is None
        assert _convention(_row("g", 31, None, 7)) is None
        assert _convention(_row("g", 31, 3, None)) is None

    def test_out_of_range_components_yield_none(self):
        assert _convention(_row("g", 31, -1, 7)) is None  # negative season
        assert _convention(_row("g", 31, 3, 0)) is None  # episode < 1
        assert _convention(_row("g", 5, 3, 7)) is None  # absolute < episode

    def test_valid_row_maps_to_season_and_offset(self):
        assert _convention(_row("g", 31, 3, 7)) == (3, 24)


# ---------------------------------------------------------------------------
# _single_convention
# ---------------------------------------------------------------------------


class TestSingleConvention:
    def test_agreeing_rows(self):
        rows = [_row("a", 30, 3, 6), _row("b", 31, 3, 7)]
        assert _single_convention(rows) == (3, 24)

    def test_conflicting_or_empty_yield_none(self):
        rows = [_row("a", 30, 3, 6), _row("b", 31, 2, 7)]
        assert _single_convention(rows) is None
        # No usable rows at all (all conventions None).
        assert _single_convention([_row("a", None, 3, 6)]) is None


# ---------------------------------------------------------------------------
# _choose_convention evidence policy
# ---------------------------------------------------------------------------


class TestChooseConvention:
    def test_same_group_manual_single_convention_wins(self):
        rows = [_row("Nix-Raws", 31, 3, 7, "manual")]
        assert _choose_convention(rows, "nix-raws") == (3, 24)

    def test_same_group_conflicting_manuals_fall_through_to_none(self):
        rows = [
            _row("G", 30, 3, 6, "manual"),
            _row("G", 31, 2, 7, "manual"),
        ]
        assert _choose_convention(rows, "g") is None

    def test_same_group_needs_two_distinct_absolutes_without_manual(self):
        rows = [
            _row("G", 30, 3, 6, "reconciled"),
            _row("G", 31, 3, 7, "reconciled"),
        ]
        assert _choose_convention(rows, "g") == (3, 24)

    def test_single_group_single_absolute_is_not_enough(self):
        rows = [_row("G", 30, 3, 6, "reconciled")]
        assert _choose_convention(rows, "g") is None

    def test_cross_group_fallback_requires_two_groups_or_two_absolutes(self):
        rows = [
            _row("A", 30, 3, 6, "reconciled"),
            _row("B", 31, 3, 7, "reconciled"),
        ]
        assert _choose_convention(rows, "c") == (3, 24)

    def test_empty_target_group_skips_same_group_scan(self):
        # No target group: the cross-group policy applies directly.
        rows = [
            _row("A", 30, 3, 6, "reconciled"),
            _row("B", 31, 3, 7, "reconciled"),
        ]
        assert _choose_convention(rows, None) == (3, 24)
        # A single agreeing group with one example is still not enough.
        assert _choose_convention([_row("A", 30, 3, 6, "reconciled")], None) is None

    def test_plural_target_group_matches_compound_row_label(self):
        # The target carries the split list; a sibling keeps the compound
        # scalar label — both canonicalize to the same group set.
        rows = [_row("A&B", 31, 3, 7, "manual")]
        assert _choose_convention(rows, ["a", "b"]) == (3, 24)


# ---------------------------------------------------------------------------
# apply_season_history_default with caller-supplied history rows
# ---------------------------------------------------------------------------


class TestSeasonHistoryDefaultWithRows:
    async def test_manual_variant_wins_over_conflicting_raw_rows(self):
        target = _target(episode=7)
        rows = [
            _row("Group", None, 2, 7, "manual"),
            _row("Group", None, 1, 7, "raw"),
            _row("Group", None, 3, 7, "raw"),
        ]
        assert await apply_season_history_default(None, target, history_rows=rows) is True
        assert target.season == 2
        # ambiguous resources are promoted once the season is history-backed
        assert target.episode_confidence == "reconciled"

    async def test_two_agreeing_structured_rows_default_season(self):
        target = _target(episode=7, episode_confidence="raw")
        rows = [
            _row("Group", None, 4, 7, "raw", id="r1"),
            _row("Group", None, 4, 7, "reconciled", id="r2"),
        ]
        assert await apply_season_history_default(None, target, history_rows=rows) is True
        assert target.season == 4
        # raw confidence is kept — only ambiguous is promoted
        assert target.episode_confidence == "raw"

    async def test_conflicting_structured_rows_never_guess(self):
        target = _target(episode=7)
        rows = [
            _row("Group", None, 1, 7, "raw", id="r1"),
            _row("Group", None, 2, 7, "raw", id="r2"),
        ]
        assert await apply_season_history_default(None, target, history_rows=rows) is False
        assert target.season is None

    async def test_rows_are_filtered_to_same_work_episode_and_group(self):
        target = _target(episode=7)
        rows = [
            _row("Group", None, 2, 7, "manual", id="self"),  # placeholder, replaced below
            _row("Group", None, 2, 8, "manual", id="other-episode"),
            _row("Group", None, 2, 7, "manual", series_id="s2", id="other-series"),
            _row("Group", None, 2, 7, "manual", channel_id="ch2", id="other-channel"),
            _row("Group", None, 2, 7, "manual", is_batch=True, id="batch"),
            _row("Group", None, None, 7, "manual", id="no-season"),
            _row("Group", None, 2, 7, "ambiguous", id="untrusted"),
            _row("Other", None, 2, 7, "manual", id="other-group"),
        ]
        rows[0].id = target.id  # the resource's own row never counts
        assert await apply_season_history_default(None, target, history_rows=rows) is False
        assert target.season is None

    async def test_ungrouped_target_accepts_any_group(self):
        target = _target(subtitle_group=None, episode=7)
        rows = [_row("Any", None, 2, 7, "manual")]
        assert await apply_season_history_default(None, target, history_rows=rows) is True
        assert target.season == 2


# ---------------------------------------------------------------------------
# apply_episode_history_reconcile with caller-supplied history rows
# ---------------------------------------------------------------------------


class TestEpisodeHistoryReconcileWithRows:
    async def test_adjacent_manual_convention_reconciles(self):
        target = _target(season=1, episode=32)
        rows = [_row("Group", 31, 3, 7, "manual")]
        changed = await apply_episode_history_reconcile(
            None, target, seasons_map={3: 12}, history_rows=rows
        )
        assert changed is True
        assert (target.season, target.episode, target.absolute_episode) == (3, 8, 32)
        assert target.episode_confidence == "reconciled"

    async def test_target_absolute_prefers_recorded_absolute_episode(self):
        target = _target(season=1, episode=2, absolute_episode=32)
        rows = [_row("Group", 31, 3, 7, "manual")]
        changed = await apply_episode_history_reconcile(
            None, target, seasons_map={3: 12}, history_rows=rows
        )
        assert changed is True
        assert (target.season, target.episode) == (3, 8)

    async def test_invalid_target_absolute_returns_false(self):
        target = _target(season=1, episode=2, absolute_episode=-3)
        assert await apply_episode_history_reconcile(
            None, target, history_rows=[_row("Group", 30, 3, 6, "manual")]
        ) is False

    async def test_only_earlier_absolutes_within_distance_are_used(self):
        target = _target(season=1, episode=32)
        rows = [
            _row("Group", 32, 3, 8, "manual", id="peer"),  # same absolute: excluded
            _row("Group", 29, 3, 5, "reconciled", id="too-old"),  # below lower bound, non-manual: excluded
            _row("Group", 31, 3, 7, "raw", id="untrusted"),  # raw: excluded
            _row("Group", 31, 3, 7, "manual", series_id="s2", id="other-series"),
            _row("Group", 31, 3, 7, "manual", channel_id="ch2", id="other-channel"),
            _row("Group", 31, 3, 7, "manual", is_batch=True, id="batch"),
            _row("Group", None, 3, 7, "manual", id="no-absolute"),
            _row("Group", 31, None, 7, "manual", id="no-season"),
            _row("Group", 31, 3, None, "manual", id="no-episode"),
        ]
        rows.append(_row("Group", 31, 3, 7, "manual", id=target.id))  # self: excluded
        assert await apply_episode_history_reconcile(None, target, history_rows=rows) is False
        assert (target.season, target.episode) == (1, 32)

    async def test_no_eligible_rows_returns_false(self):
        target = _target(season=1, episode=32)
        assert await apply_episode_history_reconcile(None, target, history_rows=[]) is False

    async def test_rows_without_choosable_convention_return_false(self):
        """Eligible rows exist, but the evidence policy cannot pick a
        convention: a single cross-group, non-manual example is not enough."""
        target = _target(season=1, episode=32)
        rows = [_row("Other", 31, 3, 7, "reconciled")]
        assert await apply_episode_history_reconcile(
            None, target, seasons_map={3: 12}, history_rows=rows
        ) is False
        assert (target.season, target.episode) == (1, 32)

    async def test_episode_beyond_season_count_plus_tolerance_rejected(self):
        target = _target(season=1, episode=32)
        rows = [_row("Group", 31, 3, 7, "manual")]
        # Convention gives S3E8 but season 3 is known to have 5 episodes
        # (8 > 5 + tolerance 2).
        assert await apply_episode_history_reconcile(
            None, target, seasons_map={3: 5}, history_rows=rows
        ) is False
        assert (target.season, target.episode) == (1, 32)

    async def test_noop_when_resource_already_matches_convention(self):
        target = _target(
            season=3, episode=8, absolute_episode=32, episode_confidence="reconciled"
        )
        rows = [_row("Group", 31, 3, 7, "manual")]
        assert await apply_episode_history_reconcile(
            None, target, seasons_map={3: 12}, history_rows=rows
        ) is False

    async def test_manual_target_is_never_touched(self):
        target = _target(season=1, episode=32, episode_confidence="manual")
        rows = [_row("Group", 31, 3, 7, "manual")]
        assert await apply_episode_history_reconcile(None, target, history_rows=rows) is False
