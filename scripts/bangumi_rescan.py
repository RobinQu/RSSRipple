"""Bangumi re-scan for legacy Wikipedia-identified TV works.

No channel uses ``source='wikipedia'`` anymore (channels run bangumi/tmdb);
works whose primary identity is a Wikipedia page are legacy rows whose
metadata (dates, episode counts, posters …) the wikipedia pipeline filled
only partially — e.g. missing ``start_date`` keeps their resources in
文件资源元数据确认 on the ``year`` required-field gate. This script re-scans
each such TV work against the **Bangumi** source through the SAME pipeline
the UI manual refresh uses (``refresh_work_by_source`` →
``search_metadata_via_llm`` → bangumi search/judge/details, season-aware via
the work's own ``season_number``) and:

  * fills fields that are still EMPTY on the work (creator-wins: the
    wikipedia primary id is kept; manual edits are respected);
  * bags the matched ``bangumi:{id}`` into the work's identity bag
    (``work_external_ids``) so future bangumi-sourced ingestion reverse-
    lookup converges on this work instead of creating a duplicate season
    work. An id already owned by ANOTHER work is reported as a dedup
    candidate (merge via ``POST /works/merge``), never stolen.

It also re-files mis-sourced bag rows (``source='wikipedia'`` holding a
self-declaring ``bangumi:…``/``tmdb:…`` id — legacy fallback writes) under
their declared source.

Movies are OUT OF SCOPE: the bangumi search is restricted to ``type=[2]``
(TV animation), so wikipedia-primary Movie rows are only reported.

DRY-RUN by default (search + report only); ``--apply`` writes. Against
PostgreSQL no app stop is needed; STOP the app first for embedded Turso
(single-process file lock).

Usage:
    uv run python scripts/bangumi_rescan.py [--apply] [--limit N] [--delay S]
"""
import argparse
import asyncio

from sqlalchemy import select

from app.database import async_session_factory
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_external_id import WorkExternalId
from app.services.bangumi_client import bangumi_configured
from app.services.external_ids import add_external_id
from app.services.metadata_search import refresh_work_by_source
from app.services.metadata_service import search_metadata_via_llm
from app.services.metadata_source_registry import REGISTRY_SOURCES
from app.services.runtime_config import load_runtime_config

_FILLABLE = [
    "title_cn", "title_en", "original_title", "description", "rating",
    "status", "genre", "poster_url", "number_of_episodes",
    "start_date", "end_date",
]


async def select_legacy_works(db) -> list[TVSeries]:
    """TV works whose PRIMARY identity is a wikipedia page (legacy rows)."""
    return list((await db.execute(
        select(TVSeries)
        .where(TVSeries.external_source == "wikipedia")
        .order_by(TVSeries.created_at)
    )).scalars().all())


async def find_misplaced_bag_rows(db) -> list[WorkExternalId]:
    """Bag rows whose id self-declares a DIFFERENT registry prefix than the
    ``source`` column (e.g. source='wikipedia' holding 'bangumi:123')."""
    rows = list((await db.execute(select(WorkExternalId))).scalars().all())
    out = []
    for row in rows:
        ext = row.external_id or ""
        if ":" not in ext:
            continue
        prefix = ext.split(":", 1)[0]
        if prefix != row.source and prefix in REGISTRY_SOURCES:
            out.append(row)
    return out


async def fix_misplaced_bag_rows(db, rows: list[WorkExternalId], apply: bool) -> None:
    for row in rows:
        line = (f"  bag re-file: {row.work_type} {row.work_id[:8]}… "
                f"{row.source}:{row.external_id} → declared source")
        if apply:
            await add_external_id(db, row.work_type, row.work_id, row.source, row.external_id)
            # ^ _canonical re-files self-declaring prefixes under the declared
            # source (creator-wins, no stealing on conflict).
            await db.delete(row)
            print(line + "  <- re-filed")
        else:
            print(line)
    if apply and rows:
        await db.commit()


def _pick_best(candidates: list[dict]) -> dict | None:
    usable = [
        c for c in candidates
        if isinstance(c, dict)
        and any(c.get(k) for k in ("title_cn", "title_en", "original_title", "canonical_name"))
    ]
    if not usable:
        return None
    return next((c for c in usable if c.get("content_type") == "tv"), usable[0])


def _empty_fields(work: TVSeries) -> list[str]:
    return [f for f in _FILLABLE if getattr(work, f) in (None, "", [], ())]


async def main(limit: int | None, delay: float, apply: bool, llm_base_url: str | None, skip: int) -> None:
    async with async_session_factory() as db:
        await load_runtime_config(db)
        if llm_base_url:
            # Host-run escape hatch: the DB override may hold a container-only
            # address (host.docker.internal) that does not resolve off-cluster.
            from app.services import runtime_config as _rc

            _rc._overrides["llm_base_url"] = llm_base_url
        if not bangumi_configured():
            print("bangumi is not configured (no token) — aborting")
            return

        misplaced = await find_misplaced_bag_rows(db)
        print(f"{len(misplaced)} mis-sourced bag rows"
              + ("" if apply else " (dry-run, not re-filed)"))
        await fix_misplaced_bag_rows(db, misplaced, apply)

        movies = (await db.execute(
            select(Movie.id).where(Movie.external_source == "wikipedia")
        )).scalars().all()
        print(f"{len(movies)} wikipedia-primary MOVIES skipped "
              "(bangumi search is TV-only)")

        works = await select_legacy_works(db)
        if skip:
            works = works[skip:]
        if limit:
            works = works[:limit]
        print(f"\n{len(works)} wikipedia-primary TV works to re-scan via bangumi"
              + (f" (skipping first {skip})" if skip else "")
              + ("" if apply else " (dry-run)"))

        ok = no_match = 0
        bagged = 0
        dedup_candidates: list[str] = []
        for i, work in enumerate(works, 1):
            label = work.title_cn or work.title_en or work.original_title or work.id[:8]
            head = f"[{i}/{len(works)}] {label} S{work.season_number}"
            if apply:
                rep = await refresh_work_by_source(
                    db, work, "tv", "bangumi", only_missing=True,
                )
                cand = rep.get("candidate") or {}
                ext_id = cand.get("external_id")
                if not ext_id:
                    no_match += 1
                    print(f"{head}: NO MATCH ({rep.get('message')})")
                else:
                    ok += 1
                    filled = ",".join(rep.get("applied") or []) or "-"
                    line = f"{head}: {ext_id} ({cand.get('title_cn') or cand.get('title_en')}) filled={filled}"
                    # apply_work_metadata already bags the candidate identity
                    # under its own source; an identity_conflict result means
                    # the id is owned by ANOTHER work (dedup candidate).
                    if rep.get("identity_conflict"):
                        dedup_candidates.append(f"{label} S{work.season_number} ({work.id})")
                        line += f"  <- DEDUP CANDIDATE: {ext_id} owned by another work"
                    else:
                        bagged += 1
                        line += "  <- bagged"
                    print(line)
            else:
                title = work.title_en or work.title_cn or work.original_title
                best = _pick_best(await search_metadata_via_llm(
                    title, "bangumi", season_hint=work.season_number,
                ))
                if not best:
                    no_match += 1
                    print(f"{head}: NO MATCH")
                else:
                    ok += 1
                    gaps = ",".join(_empty_fields(work)) or "-"
                    print(f"{head}: {best.get('external_id')} "
                          f"({best.get('title_cn') or best.get('title_en')}) "
                          f"date={best.get('start_date')} eps={best.get('number_of_episodes')} "
                          f"empty-fields={gaps}")
            if delay:
                await asyncio.sleep(delay)

        print(f"\ndone: {ok} matched, {no_match} no-match"
              + (f", {bagged} bagged" if apply else " (dry-run)"))
        if dedup_candidates:
            print("dedup candidates (merge via POST /works/merge):")
            for d in dedup_candidates:
                print(f"  {d}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write results (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="process at most N works")
    parser.add_argument("--skip", type=int, default=0, help="skip the first N selected works")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between works")
    parser.add_argument("--llm-base-url", default=None,
                        help="override the DB-persisted llm_base_url "
                             "(e.g. http://localhost:8000/v1 when running on the host)")
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.delay, args.apply, args.llm_base_url, args.skip))
