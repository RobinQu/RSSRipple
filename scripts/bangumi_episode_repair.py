"""Repair Episode rows polluted by Bangumi continuation (absolute) numbering.

Later-season Bangumi subjects often number their main-story episodes
continuing from previous seasons (``sort`` = global number, e.g. a season-3
subject listing 25-36). The metadata pipeline used to persist ``sort``
verbatim, so season works accumulated Episode rows numbered past their own
``number_of_episodes`` — duplicating the season's real episodes under
absolute numbers, or (worse) replacing season-local rows entirely.

This script rebuilds each affected work's episode list into season-local
numbering:

  * online (default, needs a bangumi identity on the work): refetch the
    subject's episode list and use the season-local ``ep`` number (+ real
    ``airdate``) as authority — the same mapping ``_episode_list_from`` now
    applies in the pipeline. Rows outside the authoritative range are
    deleted; rows inside are kept and only get NULL/placeholder fields
    filled (existing titles/air dates are never overwritten); missing rows
    are inserted.
  * ``--offline``: no network. Rebase only when every existing row is
    continuation-numbered (min episode > 1 and the row count equals
    ``number_of_episodes``); the block is shifted down so it starts at 1.

Also repairs a stale ``number_of_episodes`` when the authoritative list is
longer (still-airing shows whose count lagged), unless the field was
manually edited.

DRY-RUN by default; ``--apply`` writes.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock - STOP the app before running this against a Turso dev database.
Against PostgreSQL no stop is needed.

Usage:
    uv run python scripts/bangumi_episode_repair.py [--apply] [--offline] \
        [--series-id ID ...] [--limit N]
"""
import argparse
import asyncio
import re
from datetime import date

import httpx
from sqlalchemy import func, select

from app.database import async_session_factory
from app.models.episode import Episode
from app.models.series import TVSeries
from app.models.work_external_id import WorkExternalId
from app.services.bangumi_client import get_subject_episodes
from app.services.metadata_bangumi import _episode_list_from
from app.services.metadata_episode_reconcile import _RECONCILE_TOLERANCE
from app.services.metadata_service import manually_edited_fields

APPLY_BATCH_SIZE = 10

_PLACEHOLDER_TITLE_RE = re.compile(
    r"^(第\s*\d+\s*[集话回]|episode\s*\d+|\d+)$", re.IGNORECASE
)


def _parse_airdate(value) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _title_needs_fill(existing: str | None, incoming: str | None) -> bool:
    """Fill only missing or machine-generated placeholder titles — a real
    curated title is never overwritten."""
    if not incoming:
        return False
    return not existing or bool(_PLACEHOLDER_TITLE_RE.match(existing.strip()))


async def _bangumi_subject_id(db, work: TVSeries) -> str | None:
    if work.external_source == "bangumi" and work.external_id:
        sid = work.external_id.rsplit(":", 1)[-1]
        if sid.isdigit():
            return sid
    rows = (await db.execute(select(WorkExternalId).where(
        WorkExternalId.work_type == "series",
        WorkExternalId.work_id == work.id,
        WorkExternalId.source == "bangumi",
    ))).scalars().all()
    for row in rows:
        sid = row.external_id.rsplit(":", 1)[-1]
        if sid.isdigit():
            return sid
    return None


async def select_candidate_works(db) -> list[TVSeries]:
    """Works whose Episode rows exceed the season-local envelope."""
    rows = (await db.execute(
        select(
            Episode.series_id,
            func.min(Episode.episode),
            func.max(Episode.episode),
        ).group_by(Episode.series_id)
    )).all()
    if not rows:
        return []
    works = {
        w.id: w
        for w in (await db.execute(
            select(TVSeries).where(TVSeries.id.in_([r[0] for r in rows]))
        )).scalars().all()
    }
    out = []
    for series_id, lo, hi in rows:
        work = works.get(series_id)
        if work is None:
            continue
        count = work.number_of_episodes
        if lo > 1 or (
            isinstance(count, int) and count > 0 and hi > count + _RECONCILE_TOLERANCE
        ):
            out.append(work)
    out.sort(key=lambda w: w.created_at)
    return out


async def _desired_episode_list(
    db, work: TVSeries, client, *, online: bool, rows: list[Episode]
) -> tuple[list[dict], str | None, str | None]:
    """The authoritative season-local entries: (desired, source, note)."""
    note = None
    if online:
        sid = await _bangumi_subject_id(db, work)
        if sid is None:
            note = "no bangumi identity; trying offline heuristic"
        else:
            try:
                raw = await get_subject_episodes(client, sid)
                desired = _episode_list_from(raw, work.season_number or 1)
                if desired:
                    return desired, f"bangumi:{sid}", note
                note = f"bangumi:{sid} returned no usable episodes"
            except Exception as e:  # network/HTTP failure — fall back
                note = f"bangumi:{sid} fetch failed: {e}"
    lo = min(r.episode for r in rows)
    count = work.number_of_episodes
    if lo > 1 and isinstance(count, int) and count > 0 and len(rows) == count:
        shift = lo - 1
        desired = [
            {"episode": r.episode - shift, "title": r.title, "air_date": r.air_date}
            for r in rows
        ]
        return desired, f"offline rebase (-{shift})", note
    return [], None, note


async def plan_work(db, work: TVSeries, client, *, online: bool) -> dict:
    """Compute the repair plan for one work. Pure read - no DB writes, so the
    --apply path can call this then persist the result."""
    report = {
        "id": work.id,
        "title": work.title_cn or work.title_en,
        "season": work.season_number,
        "count": work.number_of_episodes,
    }
    rows = list((await db.execute(
        select(Episode).where(Episode.series_id == work.id).order_by(Episode.episode)
    )).scalars().all())
    report["existing"] = [r.episode for r in rows]
    if not rows:
        return report | {"ok": False, "reason": "no episode rows"}

    desired, source, note = await _desired_episode_list(
        db, work, client, online=online, rows=rows
    )
    if note:
        report["note"] = note
    if not desired:
        return report | {
            "ok": False,
            "reason": "no authoritative episode list and offline rebase "
                      "heuristic does not apply (rows are not a pure "
                      "continuation block matching the episode count)",
        }

    wanted = {d["episode"]: d for d in desired}
    have = {r.episode for r in rows}
    deletions = sorted(have - wanted.keys())
    inserts = sorted(wanted.keys() - have)
    fills = sum(
        1 for r in rows if r.episode in wanted and (
            _title_needs_fill(r.title, wanted[r.episode].get("title"))
            or (r.air_date is None
                and _parse_airdate(wanted[r.episode].get("air_date")) is not None)
        )
    )

    max_ep = max(wanted)
    count = work.number_of_episodes
    new_count = None
    if (
        (not isinstance(count, int) or count < max_ep)
        and "number_of_episodes" not in manually_edited_fields(work)
    ):
        new_count = max_ep

    return report | {
        "ok": True,
        "source": source,
        "delete": deletions,
        "insert": inserts,
        "fill": fills,
        "new_count": new_count,
        "desired": sorted(wanted),
        "_desired": desired,
    }


def apply_plan(work: TVSeries, rows: list[Episode], rep: dict, db) -> list[str]:
    """Persist one work's repair plan. Returns the list of change tags."""
    changed: list[str] = []
    wanted = {d["episode"]: d for d in rep["_desired"]}
    for r in rows:
        d = wanted.get(r.episode)
        if d is None:
            continue
        if _title_needs_fill(r.title, d.get("title")):
            r.title = d["title"]
            changed.append(f"title:{r.episode}")
        d_air = _parse_airdate(d.get("air_date"))
        if r.air_date is None and d_air is not None:
            r.air_date = d_air
            changed.append(f"air_date:{r.episode}")
    for r in rows:
        if r.episode in rep["delete"]:
            changed.append(f"delete:{r.episode}")  # deleted by the caller (await db.delete)
    for n in rep["insert"]:
        d = wanted[n]
        db.add(Episode(
            series_id=work.id,
            season=work.season_number or 1,
            episode=n,
            title=d.get("title"),
            air_date=_parse_airdate(d.get("air_date")),
        ))
        changed.append(f"insert:{n}")
    if rep.get("new_count"):
        work.number_of_episodes = rep["new_count"]
        changed.append(f"number_of_episodes:{rep['new_count']}")
    return changed


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--offline", action="store_true", help="no network; rebase heuristic only")
    parser.add_argument("--series-id", action="append", default=[], help="restrict to work id(s)")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    async with async_session_factory() as db:
        if args.series_id:
            works = list((await db.execute(
                select(TVSeries).where(TVSeries.id.in_(args.series_id))
            )).scalars().all())
        else:
            works = await select_candidate_works(db)
        if args.limit:
            works = works[: args.limit]
        print(f"{len(works)} candidate work(s); mode={'APPLY' if args.apply else 'DRY-RUN'}"
              f"{' offline' if args.offline else ''}")

        client = None if args.offline else httpx.AsyncClient(timeout=20)
        try:
            done = skipped = 0
            for i, work in enumerate(works, 1):
                rep = await plan_work(db, work, client, online=not args.offline)
                head = (f"[{i}/{len(works)}] {rep['title']} S{rep['season']} "
                        f"count={rep['count']} existing={rep['existing']}")
                if not rep.get("ok"):
                    skipped += 1
                    print(f"{head}\n    SKIP: {rep['reason']} {rep.get('note', '')}")
                    continue
                done += 1
                print(f"{head}\n    source={rep['source']} desired={rep['desired']}"
                      f"\n    delete={rep['delete']} insert={rep['insert']}"
                      f" fill={rep['fill']} new_count={rep['new_count']}")
                if args.apply:
                    rows = list((await db.execute(
                        select(Episode).where(Episode.series_id == work.id)
                    )).scalars().all())
                    awaitables = apply_plan(work, rows, rep, db)
                    for r in rows:
                        if r.episode in rep["delete"]:
                            await db.delete(r)
                    print(f"    applied: {len(awaitables)} change(s)")
                    if i % APPLY_BATCH_SIZE == 0:
                        await db.commit()
            if args.apply:
                await db.commit()
        finally:
            if client is not None:
                await client.aclose()
        print(f"done={done} skipped={skipped}; "
              f"{'changes committed' if args.apply else 'dry-run only — rerun with --apply'}")


if __name__ == "__main__":
    asyncio.run(main())
