"""One-off repair: fill missing episode/season on FileResources using the
universal fallback extractor (``extract_episode_fallback``), then re-run
episode reconciliation with the linked series' persisted seasons.

Why: the per-channel field_mapping episode regexes typically only cover the
"- NN" form, so bracket-numbered releases ("[03]") and some season markers
("Season 3", 第N季) were never parsed; resources that linked via the
agent-free paths then kept ``episode=None`` (or a defaulted season=1)
forever. The fallback now runs at fetch time via ``normalize_parsed_fields``;
this script repairs rows created before that.

Only resources with ``episode_confidence IS NULL`` (never vetted by reconcile
or a human) are touched; season is overwritten only when the title carries an
explicit season marker that disagrees with the stored value.

NOTE on locking: stop the app (``docker compose stop app``) before running
against the dev database (Turso single-process file lock), then start it
again afterwards. Dry-run by default; pass --apply to write.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.database import async_session_factory
from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.services.metadata_episode_reconcile import (
    apply_episode_reconcile,
    seasons_map_from_list,
)
from app.services.resource_parser import extract_episode_fallback


async def main(apply: bool, channel_id: str | None) -> None:
    print(f"=== episode/season parse repair ({'APPLY' if apply else 'DRY-RUN'}) ===")
    async with async_session_factory() as db:
        stmt = select(FileResource).where(
            FileResource.is_batch.is_(False),
            FileResource.episode_confidence.is_(None),
        )
        if channel_id:
            stmt = stmt.where(FileResource.channel_id == channel_id)
        rows = (await db.execute(stmt)).scalars().all()
        print(f"scanning {len(rows)} unvetted resources")

        n_ep = n_season = n_reconciled = 0
        for r in rows:
            fb_ep, fb_season = extract_episode_fallback(r.title_raw or "")
            changed = []
            if r.episode is None and fb_ep is not None:
                r.episode = fb_ep
                changed.append(f"episode={fb_ep}")
                n_ep += 1
            if fb_season is not None and r.season != fb_season:
                changed.append(f"season {r.season}->{fb_season}")
                r.season = fb_season
                n_season += 1
            if not changed:
                continue
            # With the episode/season filled, reconcile against the series'
            # per-season counts (e.g. absolute 89 -> per-season 17).
            if r.series_id:
                series = await db.get(TVSeries, r.series_id)
                smap = seasons_map_from_list(series.seasons if series else None)
                if smap and apply_episode_reconcile(r, smap):
                    changed.append(f"reconciled->S{r.season}E{r.episode}({r.episode_confidence})")
                    n_reconciled += 1
            print(f"  [{','.join(changed)}] | {(r.title_raw or '')[:70]}")
        print(f"episode-filled={n_ep} season-fixed={n_season} reconciled={n_reconciled}")

        if apply:
            await db.commit()
            print("committed.")
        else:
            print("dry-run: nothing written; re-run with --apply to execute.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--channel", default=None, help="limit to one channel id")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.channel))
