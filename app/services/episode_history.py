"""History-backed season/episode reconciliation for linked TV resources.

Some release groups publish cumulative episode numbers while still spelling a
fixed (and misleading) ``S01`` in every title.  A previously hand-corrected
resource is stronger evidence for that group's convention than the raw marker
on the next release.  This module keeps that database-aware policy separate
from the pure arithmetic helpers in :mod:`metadata_episode_reconcile`.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file_resource import FileResource
from app.services.metadata_episode_reconcile import _RECONCILE_TOLERANCE
from app.services.subtitle_groups import canonical_group_set

logger = logging.getLogger(__name__)

_MAX_ABSOLUTE_DISTANCE = 2
_HISTORY_SCAN_LIMIT = 60
_FAR_SCAN_LIMIT = 200


def _group_key(value: object) -> frozenset[str]:
    """Stable release-group identity for scalar and plural values."""
    return canonical_group_set(value)


def _convention(row: FileResource) -> tuple[int, int] | None:
    """Return ``(season, absolute-to-season offset)`` for a history row."""
    if row.absolute_episode is None or row.season is None or row.episode is None:
        return None
    offset = row.absolute_episode - row.episode
    if row.season < 0 or row.episode < 1 or offset < 0:
        return None
    return row.season, offset


def _single_convention(rows: list[FileResource]) -> tuple[int, int] | None:
    conventions = {_convention(row) for row in rows}
    conventions.discard(None)
    if len(conventions) != 1:
        return None
    return next(iter(conventions))


def _choose_convention(
    rows: list[FileResource], target_group: str, *, require_manual: bool = False
) -> tuple[int, int] | None:
    """Choose a convention using the strict evidence policy.

    An adjacent manual correction from the same release group is sufficient.
    Without one, the same group needs two distinct absolute examples.  The
    cross-group fallback requires either two groups or two distinct absolute
    examples, and every eligible example must agree.

    ``require_manual`` (far extrapolation beyond the adjacency window) —
    only human-vetted (``manual``) rows establish the convention: they must
    all agree on a single convention, and any non-manual history that
    disagrees vetoes it. One manual correction is enough, even cross-group —
    the per-season-count sanity check downstream bounds the blast radius.
    """
    if require_manual:
        manual_rows = [row for row in rows if row.episode_confidence == "manual"]
        convention = _single_convention(manual_rows)
        if convention is None:
            return None
        everything = _single_convention(rows)
        if everything is not None and everything != convention:
            return None
        return convention
    target_group = _group_key(target_group)
    same_group = (
        [row for row in rows if _group_key(row.subtitle_group) == target_group]
        if target_group
        else []
    )
    convention: tuple[int, int] | None = None
    if same_group:
        manual = [row for row in same_group if row.episode_confidence == "manual"]
        if manual:
            convention = _single_convention(manual)
        if convention is None and len({row.absolute_episode for row in same_group}) >= 2:
            convention = _single_convention(same_group)

    if convention is None:
        convention = _single_convention(rows)
        if convention is not None:
            groups = {_group_key(row.subtitle_group) for row in rows if _group_key(row.subtitle_group)}
            absolutes = {row.absolute_episode for row in rows}
            if not (len(groups) >= 2 or len(absolutes) >= 2):
                convention = None
    return convention


async def apply_season_history_default(
    db: AsyncSession,
    resource: Any,
    *,
    history_rows: list[FileResource] | None = None,
) -> bool:
    """Fill a missing season from trusted same-work release history.

    Exact same-episode variants from one release group are the strongest
    evidence (e.g. 简体/繁体/简繁 editions of E07).  One manual correction is
    sufficient; otherwise at least two structured sibling rows must agree on
    the season.  This never guesses across conflicting seasons and never
    touches a manual or batch resource.
    """
    if (
        not getattr(resource, "series_id", None)
        or getattr(resource, "movie_id", None)
        or getattr(resource, "is_batch", False)
        or getattr(resource, "season", None) is not None
        or getattr(resource, "episode", None) is None
        or getattr(resource, "episode_confidence", None) == "manual"
    ):
        return False

    target_group = _group_key(getattr(resource, "subtitle_group", None))
    if history_rows is None:
        conditions = [
            FileResource.series_id == resource.series_id,
            FileResource.channel_id == resource.channel_id,
            FileResource.id != resource.id,
            FileResource.is_batch.is_(False),
            FileResource.episode == resource.episode,
            FileResource.season.isnot(None),
            FileResource.episode_confidence.in_(["manual", "reconciled", "raw"]),
        ]
        if target_group:
            conditions.append(FileResource.subtitle_group.ilike(
                getattr(resource, "subtitle_group", "").strip()
            ))
        rows = list((await db.execute(
            select(FileResource)
            .where(*conditions)
            .order_by(FileResource.created_at.desc())
            .limit(_HISTORY_SCAN_LIMIT)
        )).scalars().all())
    else:
        rows = [
            row for row in history_rows
            if row.id != resource.id
            and row.series_id == resource.series_id
            and row.channel_id == resource.channel_id
            and not row.is_batch
            and row.episode == resource.episode
            and row.season is not None
            and row.episode_confidence in ("manual", "reconciled", "raw")
            and (
                not target_group
                or _group_key(row.subtitle_group) == target_group
            )
        ][:_HISTORY_SCAN_LIMIT]
    if not rows:
        return False

    manual_seasons = {
        row.season for row in rows if row.episode_confidence == "manual"
    }
    seasons = {row.season for row in rows}
    if len(manual_seasons) == 1:
        season = next(iter(manual_seasons))
    elif len(rows) >= 2 and len(seasons) == 1:
        season = next(iter(seasons))
    else:
        return False

    resource.season = season
    if getattr(resource, "episode_confidence", None) == "ambiguous":
        resource.episode_confidence = "reconciled"
    logger.info(
        "[episode_history] defaulted resource %s to season %s from %d sibling(s)",
        getattr(resource, "id", "?"), season, len(rows),
    )
    return True


async def apply_episode_history_reconcile(
    db: AsyncSession,
    resource: Any,
    *,
    seasons_map: dict[int, int] | None = None,
    history_rows: list[FileResource] | None = None,
) -> bool:
    """Apply a trusted sibling numbering convention to ``resource``.

    Two evidence tiers:

    1. Adjacent rows (earlier absolutes within ``_MAX_ABSOLUTE_DISTANCE``)
       under the standard policy — an extrapolation from established history
       that prevents peer rows for the same newly-seen episode from
       reinforcing one another's bad parse.
    2. Far extrapolation: when no adjacent rows exist, any trusted sibling
       (either direction, peer absolutes excluded) may establish the
       convention, but only when it is anchored by at least one human-vetted
       ``manual`` correction. This is what generalizes a one-off manual fix
       (e.g. absolute 30 → S3E6) into a durable per-group numbering rule for
       resources arriving weeks or a season apart.
    """
    if (
        not getattr(resource, "series_id", None)
        or getattr(resource, "movie_id", None)
        or getattr(resource, "is_batch", False)
        or getattr(resource, "episode_confidence", None) == "manual"
        or getattr(resource, "episode", None) is None
    ):
        return False

    target_absolute = getattr(resource, "absolute_episode", None) or resource.episode
    if not isinstance(target_absolute, int) or target_absolute < 1:
        return False

    lower_bound = max(1, target_absolute - _MAX_ABSOLUTE_DISTANCE)
    if history_rows is None:
        stmt = (
            select(FileResource)
            .where(
                FileResource.series_id == resource.series_id,
                FileResource.channel_id == resource.channel_id,
                FileResource.id != resource.id,
                FileResource.is_batch.is_(False),
                FileResource.absolute_episode.isnot(None),
                FileResource.absolute_episode >= lower_bound,
                FileResource.absolute_episode < target_absolute,
                FileResource.season.isnot(None),
                FileResource.episode.isnot(None),
                FileResource.episode_confidence.in_(["manual", "reconciled"]),
            )
            .order_by(FileResource.absolute_episode.desc(), FileResource.created_at.desc())
            .limit(_HISTORY_SCAN_LIMIT)
        )
        rows = list((await db.execute(stmt)).scalars().all())
    else:
        rows = [
            row for row in history_rows
            if row.id != resource.id
            and row.series_id == resource.series_id
            and row.channel_id == resource.channel_id
            and not row.is_batch
            and row.absolute_episode is not None
            and lower_bound <= row.absolute_episode < target_absolute
            and row.season is not None
            and row.episode is not None
            and row.episode_confidence in ("manual", "reconciled")
        ][:_HISTORY_SCAN_LIMIT]
    far = False
    if not rows:
        far = True
        if history_rows is None:
            stmt = (
                select(FileResource)
                .where(
                    FileResource.series_id == resource.series_id,
                    FileResource.channel_id == resource.channel_id,
                    FileResource.id != resource.id,
                    FileResource.is_batch.is_(False),
                    FileResource.absolute_episode.isnot(None),
                    FileResource.absolute_episode != target_absolute,
                    FileResource.season.isnot(None),
                    FileResource.episode.isnot(None),
                    FileResource.episode_confidence.in_(["manual", "reconciled"]),
                )
                .order_by(FileResource.created_at.desc())
                .limit(_FAR_SCAN_LIMIT)
            )
            rows = list((await db.execute(stmt)).scalars().all())
        else:
            rows = [
                row for row in history_rows
                if row.id != resource.id
                and row.series_id == resource.series_id
                and row.channel_id == resource.channel_id
                and not row.is_batch
                and row.absolute_episode is not None
                and row.absolute_episode != target_absolute
                and row.season is not None
                and row.episode is not None
                and row.episode_confidence in ("manual", "reconciled")
            ][:_FAR_SCAN_LIMIT]
    if not rows:
        return False

    convention = _choose_convention(
        rows,
        getattr(resource, "subtitle_groups", None) or getattr(resource, "subtitle_group", None),
        require_manual=far,
    )
    if convention is None:
        return False
    season, offset = convention
    episode = target_absolute - offset
    if episode < 1:
        return False

    season_count = (seasons_map or {}).get(season)
    if season_count is not None and episode > season_count + _RECONCILE_TOLERANCE:
        return False

    before = (
        getattr(resource, "season", None),
        getattr(resource, "episode", None),
        getattr(resource, "absolute_episode", None),
        getattr(resource, "episode_confidence", None),
    )
    after = (season, episode, target_absolute, "reconciled")
    if before == after:
        return False

    resource.season = season
    resource.episode = episode
    resource.absolute_episode = target_absolute
    resource.episode_confidence = "reconciled"
    logger.info(
        "[episode_history] reconciled resource %s from %s to S%sE%s (absolute %s)",
        getattr(resource, "id", "?"), before[:2], season, episode, target_absolute,
    )
    return True


_HEAL_SCAN_LIMIT = 100


async def heal_sibling_episodes(
    db: AsyncSession,
    resource: FileResource,
    *,
    seasons_map: dict[int, int] | None = None,
) -> list[str]:
    """Reconcile unresolved siblings after a manual episode correction.

    A fresh manual correction is the strongest convention anchor; siblings
    of the same work in the same channel still sitting in the Channel
    confirmation queue (``ambiguous``) or never reconciled (``raw``) get the
    history convention applied immediately instead of waiting for a person
    to repeat the same fix. Returns the ids of resources actually changed
    (callers commit, then enqueue targeted agent reruns). Never touches
    manual rows, batches, or other works/channels.
    """
    if (
        not getattr(resource, "series_id", None)
        or getattr(resource, "is_batch", False)
        or getattr(resource, "episode_confidence", None) != "manual"
    ):
        return []
    stmt = (
        select(FileResource)
        .where(
            FileResource.series_id == resource.series_id,
            FileResource.channel_id == resource.channel_id,
            FileResource.id != resource.id,
            FileResource.is_batch.is_(False),
            FileResource.episode.isnot(None),
            FileResource.episode_confidence.in_(["ambiguous", "raw"]),
        )
        .order_by(FileResource.created_at)
        .limit(_HEAL_SCAN_LIMIT)
    )
    siblings = list((await db.execute(stmt)).scalars().all())
    healed: list[str] = []
    for sibling in siblings:
        try:
            if await apply_episode_history_reconcile(
                db, sibling, seasons_map=seasons_map
            ):
                healed.append(sibling.id)
        except Exception:
            logger.exception(
                "[episode_history] heal failed for sibling %s", sibling.id
            )
    if healed:
        logger.info(
            "[episode_history] manual correction of %s healed %d sibling(s): %s",
            resource.id, len(healed), healed,
        )
    return healed
