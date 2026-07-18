"""One-off repair for resources mis-parsed by the multi-bracket title bug.

Before the ``normalize_parsed_fields`` post-processor existed, the LLM-generated
per-channel field_mapping regexes leaked the second bracket of multi-bracket
titles (``[Group][ViuTV粵語]幪面超人 / ...``) into ``title_cn``/``title_en``
(producing ``"[ViuTV"`` / ``"粵語]幪面超人 "``). The leaked station name then
mis-directed the metadata agent, which auto-linked some resources to the
ViuTV *TV-station* Wikipedia article and spawned a bogus series (e.g.
``7d08698e`` titled "ViuTV").

This script repairs the existing bad rows:

  1. Re-derives ``title_cn``/``title_en``/``search_title`` + tech fields for
     every resource whose extracted title leaked bracket characters, using the
     same ``normalize_parsed_fields`` logic now wired into fetch_service.
  2. Unlinks resources from bogus station-named series and re-runs the local
     series/movie matcher (deterministic, no LLM) to relink them to the
     correct work. Anything without a >=85 local match is reset to
     retry-eligible so the next fetch backfill re-matches it via the agent.
  3. Deletes the now-empty bogus series.

Idempotent. Dry-run by default; pass ``--apply`` to write.
"""
import argparse
import asyncio

from sqlalchemy import select

from app.database import async_session_factory
from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.services.metadata_service import (
    AUTO_LINK_THRESHOLD,
    match_movie_by_title,
    match_series_by_title,
)
from app.services.resource_parser import normalize_parsed_fields
from app.utils.time import utcnow

# Series known to be bogus (created from a TV-station / platform Wikipedia
# article rather than a work). Add the row id + the station name it was filed
# under. Resources linked here are unlinked and re-matched.
BOGUS_SERIES = {
    "7d08698e-77b9-4aab-8797-8bc8bab75ef6": "ViuTV",
}


def _leaked(value) -> bool:
    return value is not None and ("[" in str(value) or "]" in str(value))


async def _repair_one(db, resource) -> dict:
    """Re-derive fields for a leaky resource. Returns a change summary."""
    current = {
        "title_cn": resource.title_cn,
        "title_en": resource.title_en,
        "search_title": resource.search_title,
        "resolution": resource.resolution,
        "source": resource.source,
        "audio_codec": resource.audio_codec,
        "video_codec": resource.video_codec,
        "container": resource.container,
    }
    repaired = normalize_parsed_fields(resource.title_raw, current)
    changed = {}
    for k, v in repaired.items():
        if v != current.get(k):
            changed[k] = (current.get(k), v)
            setattr(resource, k, v)
    return changed


async def _relink(db, resource) -> str:
    """Local-match search_title against series/movie; link if >= threshold.

    Returns a human-readable outcome. Leaves the resource retry-eligible
    (failure state cleared) when no local match qualifies, so the next fetch
    backfill re-runs the LLM agent on the corrected search_title.
    """
    search_title = resource.search_title
    resource.series_id = None
    resource.movie_id = None
    resource.metadata_matched_at = None
    resource.metadata_failure_type = None
    resource.metadata_attempts = 0
    resource.last_metadata_attempt_at = None

    if not search_title:
        return "no search_title -> retry-eligible"
    series, s_ratio = await match_series_by_title(db, search_title)
    movie, m_ratio = await match_movie_by_title(db, search_title)
    if series and s_ratio >= AUTO_LINK_THRESHOLD and (movie is None or s_ratio >= m_ratio):
        resource.series_id = series.id
        resource.metadata_matched_at = utcnow()
        return f"linked series {series.id} ({series.title_en!r}, score={s_ratio})"
    if movie and m_ratio >= AUTO_LINK_THRESHOLD and (series is None or m_ratio > s_ratio):
        resource.movie_id = movie.id
        resource.metadata_matched_at = utcnow()
        return f"linked movie {movie.id} (score={m_ratio})"
    return f"no >=85 local match (best series={s_ratio}, movie={m_ratio}) -> retry-eligible"


async def main(apply: bool) -> None:
    async with async_session_factory() as db:
        # 1. Find leaky resources.
        res = await db.execute(
            select(FileResource).where(
                (FileResource.title_cn.like("%[%"))
                | (FileResource.title_cn.like("%]%"))
                | (FileResource.title_en.like("%[%"))
                | (FileResource.title_en.like("%]%"))
                | (FileResource.search_title.like("%[%"))
                | (FileResource.search_title.like("%]%"))
            )
        )
        leaky = res.scalars().all()
        print(f"Leaky resources: {len(leaky)}")

        relinked = []
        repaired = 0
        for r in leaky:
            changed = await _repair_one(db, r)
            if changed:
                repaired += 1
            bogus_link = r.series_id in BOGUS_SERIES
            if bogus_link:
                note = await _relink(db, r)
                relinked.append((r, note))
                print(f"  RELINK ep={r.episode} title_raw={r.title_raw[:60]!r}")
                for k, (before, after) in changed.items():
                    print(f"    {k}: {before!r} -> {after!r}")
                print(f"    -> {note}")
        print(f"Fields repaired on {repaired}/{len(leaky)} leaky resources.")

        # 2. Delete now-empty bogus series (only if no resources remain linked).
        for sid, name in BOGUS_SERIES.items():
            count = (
                await db.execute(
                    select(FileResource.id).where(FileResource.series_id == sid).limit(1)
                )
            ).first()
            series = await db.get(TVSeries, sid)
            if series is None:
                print(f"  bogus series {sid} ({name}): already gone")
                continue
            if count:
                print(f"  bogus series {sid} ({name}): STILL has resources linked, NOT deleting")
                continue
            print(f"  delete bogus series {sid} ({name!r})")
            if apply:
                await db.delete(series)

        if apply:
            await db.commit()
            print("\nCOMMITTED.")
        else:
            await db.rollback()
            print("\nDRY RUN (no writes). Re-run with --apply to commit.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    asyncio.run(main(args.apply))
