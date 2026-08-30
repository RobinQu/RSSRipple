"""One-off backfill: reconcile season/episode for stock series-linked resources.

Repairs FileResource rows created before the cross-season reconcile /
verified-season rules ran on every link path. Targets series-linked,
non-batch resources whose ``episode_confidence`` is ``reconciled``/``raw``/
NULL (never ``manual``, never already ``ambiguous``) and that either have no
season (``season IS NULL``) or carry both a season and an
``absolute_episode`` (consistency cross-check candidates). Each row is run
through ``apply_episode_reconcile`` with the linked series' persisted
per-season counts, then through ``resolve_missing_season`` when the season is
still unknown (single-season → ``season=1``; multi/unknown →
``episode_confidence="ambiguous"``, surfaced as a "季号不确定" PendingDecision
on the agents' next run — this script creates no PendingDecisions itself).

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass --apply to execute.
"""

import argparse
import asyncio

from sqlalchemy import and_, or_, select

from app.database import async_session_factory
from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.services.metadata_episode_reconcile import (
    apply_episode_reconcile,
    resolve_missing_season,
    season_evidence_from_series,
    seasons_map_from_list,
    verified_season_count,
)

BATCH_SIZE = 50

OUTCOME_SEASON_DERIVED = "season-derived"        # season located from absolute_episode
OUTCOME_SEASON_DEFAULTED = "season-defaulted-1"  # verified single-season work → season=1
OUTCOME_MARKED_AMBIGUOUS = "marked-ambiguous"    # 季号/集号不确定
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_SKIPPED = "skipped"                      # series carries no seasons evidence
OUTCOMES = (
    OUTCOME_SEASON_DERIVED,
    OUTCOME_SEASON_DEFAULTED,
    OUTCOME_MARKED_AMBIGUOUS,
    OUTCOME_UNCHANGED,
    OUTCOME_SKIPPED,
)


def candidate_query():
    """Stock rows eligible for the season reconcile backfill."""
    return select(FileResource).where(
        FileResource.series_id.isnot(None),
        FileResource.is_batch.is_(False),
        or_(
            FileResource.episode_confidence.is_(None),
            FileResource.episode_confidence.in_(["reconciled", "raw"]),
        ),
        or_(
            FileResource.season.is_(None),
            and_(
                FileResource.season.isnot(None),
                FileResource.absolute_episode.isnot(None),
            ),
        ),
    ).order_by(FileResource.created_at)


def reconcile_stock_resource(resource, series_row) -> str:
    """Run reconcile + season resolution for one stock resource.

    Pure decision logic (no DB) so unit tests can drive it with namespace
    doubles. Returns one of ``OUTCOMES``; the row is mutated in place for the
    non-skip outcomes.
    """
    if series_row is None:
        return OUTCOME_SKIPPED
    entity = season_evidence_from_series(series_row)
    seasons_map = seasons_map_from_list(series_row.seasons)
    if not seasons_map and verified_season_count(entity) is None:
        return OUTCOME_SKIPPED

    season_before = resource.season
    apply_episode_reconcile(resource, seasons_map)
    # Reconcile may flag the row ambiguous itself (absolute number overshoots
    # the total, or the season marker contradicts the absolute arithmetic).
    if getattr(resource, "episode_confidence", None) == "ambiguous":
        return OUTCOME_MARKED_AMBIGUOUS
    if season_before is None and resource.season is not None:
        return OUTCOME_SEASON_DERIVED
    outcome = resolve_missing_season(resource, entity)
    if outcome == "season-defaulted":
        return OUTCOME_SEASON_DEFAULTED
    if outcome == "marked-ambiguous":
        return OUTCOME_MARKED_AMBIGUOUS
    return OUTCOME_UNCHANGED


async def main(apply: bool) -> None:
    print(f"=== reconcile season backfill ({'APPLY' if apply else 'DRY-RUN'}) ===")
    async with async_session_factory() as db:
        rows = (await db.execute(candidate_query())).scalars().all()
        print(f"{len(rows)} candidate resources")

        counts = dict.fromkeys(OUTCOMES, 0)
        series_cache: dict[str, TVSeries | None] = {}
        pending = 0
        for r in rows:
            if r.series_id not in series_cache:
                series_cache[r.series_id] = await db.get(TVSeries, r.series_id)
            outcome = reconcile_stock_resource(r, series_cache[r.series_id])
            counts[outcome] += 1
            if outcome in (OUTCOME_UNCHANGED, OUTCOME_SKIPPED):
                continue
            print(
                f"  [{outcome}] {r.title_raw[:80]!r} "
                f"-> S{r.season}E{r.episode} ({r.episode_confidence})"
            )
            if apply:
                pending += 1
                if pending >= BATCH_SIZE:
                    await db.commit()
                    pending = 0

        summary = " ".join(f"{k}={v}" for k, v in counts.items())
        if apply:
            await db.commit()
            print(f"committed. {summary}")
        else:
            await db.rollback()  # in-memory mutations must not leak
            print(f"dry-run (nothing written). {summary}; re-run with --apply to execute.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
