"""Wikipedia identity backfill: rewrite languageless pageids to the qualified form.

Wikipedia pageids are per-language-edition, but rows written before the
qualifier existed store ``wikipedia:{pageid}`` (primary ``external_id`` and
identity-bag rows alike) — the edition is unrecoverable offline, display
links hardcode the en edition, and bare pageids can collide across editions
under the bag's UniqueConstraint(source, external_id).

For every series/movie with a wikipedia primary or wikipedia bag rows this
script re-derives the edition of each numeric pageid:

  1. Query all three searched editions (zh/en/ja) for the work's pageids.
     A pageid found on exactly one edition anchors there; one found on
     several editions (cross-edition numeric collision) anchors on the
     edition whose page title matches one of the work's titles/aliases.
  2. From the anchor page, fetch langlinks and resolve each language
     edition's pageid — the same identity set the pipeline bags today.
  3. Rewrite the primary ``external_id`` and bag rows to
     ``wikipedia:{lang}:{pageid}``, and fill ``wikipedia_url`` /
     ``wikipedia_page_id`` (both previously never written).

Pageids that resolve on no edition (deleted/merged pages) or that cannot be
anchored are left bare with a warning — never guessed.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app before running this against a Turso database.
PostgreSQL deployments can run it alongside the app.

Dry-run by default; pass --apply to execute.

Usage:
    uv run python scripts/wikipedia_lang_backfill.py [--apply] [--limit N] [--delay S]
"""

import argparse
import asyncio
import logging

import httpx
from sqlalchemy import select

from app.database import async_session_factory
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_external_id import WorkExternalId
from app.services.metadata_source_registry import parse_wikipedia_id

logger = logging.getLogger("wikipedia_lang_backfill")

EDITIONS = ("zh", "en", "ja")
USER_AGENT = "RSSRipple/wikipedia-lang-backfill (https://github.com/robinqu/RSSRipple)"


def _norm(title: str | None) -> str:
    return " ".join((title or "").casefold().split())


def _work_titles(work) -> set[str]:
    titles = {_norm(t) for t in (work.title_cn, work.title_en, work.original_title) if t}
    for t in work.aliases or []:
        if t:
            titles.add(_norm(t))
    return {t for t in titles if t}


async def _api(client: httpx.AsyncClient, lang: str, params: dict) -> dict:
    resp = await client.get(
        f"https://{lang}.wikipedia.org/w/api.php",
        params={"action": "query", "format": "json", **params},
    )
    resp.raise_for_status()
    return resp.json()


async def _editions_of_pids(
    client: httpx.AsyncClient, pids: list[str]
) -> dict[str, dict[str, str]]:
    """For each edition, which of ``pids`` exist and their titles.

    Returns ``{pid: {lang: title}}`` — a pid present under several langs is a
    cross-edition numeric collision and MUST be disambiguated by title.
    """
    out: dict[str, dict[str, str]] = {p: {} for p in pids}

    async def _one(lang: str) -> tuple[str, dict]:
        try:
            data = await _api(client, lang, {"pageids": "|".join(pids), "redirects": 1})
        except Exception as e:  # noqa: BLE001 - best-effort per edition
            logger.warning("pageids query failed on %s: %s", lang, e)
            return lang, {}
        return lang, (data.get("query") or {}).get("pages") or {}

    for lang, pages in await asyncio.gather(*(_one(lang) for lang in EDITIONS)):
        for pid, page in pages.items():
            if pid in out and "missing" not in page and page.get("title"):
                out[pid][lang] = page["title"]
    return out


async def _langlink_pageids(
    client: httpx.AsyncClient, anchor_lang: str, anchor_pid: str
) -> dict[str, str]:
    """{lang: pageid} for the anchor page's zh/en/ja langlinks."""
    try:
        data = await _api(
            client, anchor_lang,
            {"pageids": anchor_pid, "prop": "langlinks", "lllimit": "max"},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("langlinks query failed for %s:%s: %s", anchor_lang, anchor_pid, e)
        return {}
    pages = (data.get("query") or {}).get("pages") or {}
    links = (pages.get(anchor_pid) or {}).get("langlinks") or []
    titles = {
        link["lang"]: link["*"] for link in links
        if link.get("lang") in EDITIONS and link.get("*")
    }

    async def _resolve(lang: str, title: str) -> tuple[str, str | None]:
        try:
            data = await _api(
                client, lang, {"titles": title, "prop": "info", "redirects": 1}
            )
            for p in ((data.get("query") or {}).get("pages") or {}).values():
                pid = p.get("pageid")
                if isinstance(pid, int) and pid > 0:
                    return lang, str(pid)
        except Exception as e:  # noqa: BLE001
            logger.warning("pageid resolve failed for %s/%r: %s", lang, title, e)
        return lang, None

    resolved = await asyncio.gather(*(_resolve(lang, t) for lang, t in titles.items()))
    return {lang: pid for lang, pid in resolved if pid}


def _anchor(
    editions: dict[str, dict[str, str]], titles: set[str], primary_pid: str | None
) -> tuple[str, str] | None:
    """Pick (lang, pid) of the work's own page among the candidate pageids.

    Strong anchor: page title normalized-matches a work title/alias. Weak
    anchor: the pid exists on exactly one edition (a multi-edition pid is a
    numeric collision and needs the title match). The primary pid is
    preferred; any bag pid may anchor (merged works carry each other's).
    """
    ordered = ([primary_pid] if primary_pid else []) + [
        p for p in editions if p != primary_pid
    ]
    weak: tuple[str, str] | None = None
    for pid in ordered:
        found = editions.get(pid) or {}
        for lang, title in found.items():
            if _norm(title) in titles:
                return lang, pid
        if weak is None and len(found) == 1:
            weak = (next(iter(found)), pid)
    return weak


async def _process_work(client, db, work, bag_rows, apply: bool) -> dict:
    """Rewrite one work's wikipedia ids. Returns a stats dict."""
    stats = {"changed": 0, "skipped": 0, "warnings": 0}
    titles = _work_titles(work)

    ids: dict[str, str | None] = {}  # pid -> already-known lang (None = bare)
    primary_pid = None
    if (work.external_source or "") == "wikipedia":
        lang, pid = parse_wikipedia_id(work.external_id)
        if pid:
            ids[pid] = lang
            primary_pid = pid
    for row in bag_rows:
        lang, pid = parse_wikipedia_id(row.external_id)
        if pid:
            ids.setdefault(pid, lang)
    bare_pids = [p for p, lang in ids.items() if lang is None]
    if not bare_pids and (work.wikipedia_url or not primary_pid):
        stats["skipped"] += 1  # already fully qualified (idempotent reruns)
        return stats

    editions = await _editions_of_pids(client, list(ids))
    anchor = _anchor(editions, titles, primary_pid)
    label = work.title_cn or work.title_en or work.id
    if anchor is None:
        print(f"  [warn] {label!r}: no edition anchor for pids {sorted(ids)} — left bare")
        stats["warnings"] += 1
        return stats

    anchor_lang, anchor_pid = anchor
    recovered: dict[str, str] = {anchor_pid: anchor_lang}
    # Langlinks from the anchor page pin the other editions' pageids.
    for lang, pid in (await _langlink_pageids(client, anchor_lang, anchor_pid)).items():
        recovered.setdefault(pid, lang)
    # Single-edition pids that title-match are safe to recover directly.
    for pid, found in editions.items():
        if pid in recovered:
            continue
        if len(found) == 1:
            lang, title = next(iter(found.items()))
            if _norm(title) in titles:
                recovered[pid] = lang

    # 1) Primary external_id + wikipedia_url/page_id.
    if primary_pid and primary_pid in recovered:
        new_primary = f"wikipedia:{recovered[primary_pid]}:{primary_pid}"
        if work.external_id != new_primary:
            print(f"  primary {work.external_id} -> {new_primary}")
            stats["changed"] += 1
            if apply:
                work.external_id = new_primary
        if not work.wikipedia_url:
            url = f"https://{recovered[primary_pid]}.wikipedia.org/?curid={primary_pid}"
            print(f"  wikipedia_url -> {url}")
            stats["changed"] += 1
            if apply:
                work.wikipedia_url = url
                work.wikipedia_page_id = int(primary_pid)
    elif primary_pid:
        print(f"  [warn] {label!r}: primary pid {primary_pid} unrecovered — left bare")
        stats["warnings"] += 1

    # 2) Bag rows.
    for row in bag_rows:
        lang, pid = parse_wikipedia_id(row.external_id)
        if pid is None or lang is not None:
            continue  # slug id or already qualified
        new_lang = recovered.get(pid)
        if new_lang is None:
            print(f"  [warn] {label!r}: bag pid {pid} unrecovered — left bare")
            stats["warnings"] += 1
            continue
        new_id = f"wikipedia:{new_lang}:{pid}"
        conflict = next(
            (r for r in bag_rows if r is not row and r.external_id == new_id), None
        )
        if conflict is not None:
            # The qualified row already exists for this work — drop the bare one.
            print(f"  bag {row.external_id} -> duplicate of {new_id}, dropped")
            stats["changed"] += 1
            if apply:
                await db.delete(row)
            continue
        print(f"  bag {row.external_id} -> {new_id}")
        stats["changed"] += 1
        if apply:
            row.external_id = new_id
    return stats


async def main(apply: bool, limit: int | None, delay: float) -> None:
    print(f"=== wikipedia lang backfill ({'APPLY' if apply else 'DRY-RUN'}) ===")
    work_refs: list[tuple[str, str]] = []  # (kind, work_id)
    async with async_session_factory() as db:
        for kind, model in (("series", TVSeries), ("movie", Movie)):
            result = await db.execute(
                select(model.id, model.external_source).order_by(model.created_at)
            )
            for work_id, external_source in result.all():
                if (external_source or "") == "wikipedia":
                    work_refs.append((kind, work_id))
        bag_result = await db.execute(
            select(WorkExternalId.work_type, WorkExternalId.work_id)
            .where(WorkExternalId.source == "wikipedia")
            .distinct()
        )
        known = set(work_refs)
        for work_type, work_id in bag_result.all():
            if (work_type, work_id) not in known:
                work_refs.append((work_type, work_id))

    print(f"{len(work_refs)} works with wikipedia identity")
    if limit:
        work_refs = work_refs[:limit]

    totals = {"changed": 0, "skipped": 0, "warnings": 0, "works": 0}
    async with httpx.AsyncClient(
        timeout=15, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        for kind, work_id in work_refs:
            async with async_session_factory() as db:
                model = TVSeries if kind == "series" else Movie
                work = await db.get(model, work_id)
                if work is None:
                    continue
                bag_rows = list((await db.execute(
                    select(WorkExternalId).where(
                        WorkExternalId.work_type == kind,
                        WorkExternalId.work_id == work_id,
                        WorkExternalId.source == "wikipedia",
                    )
                )).scalars().all())
                stats = await _process_work(client, db, work, bag_rows, apply)
                if apply and stats["changed"]:
                    await db.commit()
            for k in ("changed", "skipped", "warnings"):
                totals[k] += stats[k]
            totals["works"] += 1
            if delay:
                await asyncio.sleep(delay)

    print(
        f"=== done: {totals['works']} works, {totals['changed']} changes, "
        f"{totals['warnings']} warnings, {totals['skipped']} skipped "
        f"({'applied' if apply else 'dry-run — pass --apply to write'}) ==="
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.1,
                        help="seconds between works (politeness)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main(args.apply, args.limit, args.delay))
