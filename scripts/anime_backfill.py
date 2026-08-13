"""is_anime tri-state backfill for existing TVSeries / Movie rows.

Scans works whose ``is_anime`` is still NULL and determines it from
deterministic evidence only (``app/services/anime_signals.py``), strongest
signal first:

1. **Offline identity (always runs, no network)** — the work's primary
   ``external_source`` plus every id in its identity bag
   (``WorkExternalId``) is checked against the anime-only hosts
   (bangumi / MyAnimeList / AniList). A hit sets True.
2. **Bangumi (``--bangumi``)** — layer-1 verification identical to the
   runtime post-link path: search by the work's titles (no type filter) and
   apply ``anime_signals.bangumi_verdict`` — a type-2 (anime) hit sets True,
   a type-6 (三次元) hit sets False. Needs ``BANGUMI_API_KEY``
   (``runtime_config.bangumi_api_key``); without one the phase is skipped.
3. **TMDB (``--tmdb``)** — works still undetermined whose primary identity is
   TMDB (``external_source == "tmdb"`` or ``external_id`` like
   ``tmdb:<digits>``) are queried via ``GET /3/{tv|movie}/{id}``; genre ids,
   ``original_language`` and ``origin_country`` feed ``is_anime_from_tmdb``
   (True and False are both persisted; None keeps NULL). Needs a TMDB API key
   (``runtime_config.tmdb_api_key``, i.e. ``TMDB_API_KEY`` env or the
   app_settings override); without one the phase is skipped. Verdicts are
   cached in-process to avoid duplicate requests.
4. **Wikipedia (``--wikipedia``)** — works still undetermined with a
   ``wikipedia_url`` get their wikitext fetched (language parsed from the URL
   host, zh/ja only); an animanga infobox block (``TVAnime`` for TV,
   ``Movie|Film|OVA`` for theatrical works) sets True.
   Absence of the block does NOT mean live-action, so a miss keeps NULL.

A single failing work never aborts the run — it is logged as a warning and
skipped.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass --apply to execute.

Usage:
    uv run python scripts/anime_backfill.py [--bangumi] [--tmdb] [--wikipedia] [--apply]
    uv run python scripts/anime_backfill.py --bangumi --apply [--limit N] [--delay S]
"""

import argparse
import asyncio
import logging
import re

import httpx
from sqlalchemy import select

from app.database import async_session_factory
from app.models.movie import Movie
from app.models.series import TVSeries
from app.services.anime_signals import (
    ANIME_IDENTITY_SOURCES,
    is_anime_from_tmdb,
    is_anime_identity,
)
from app.services.external_ids import list_external_ids
from app.services.metadata_wikipedia_client import fetch_wikipedia_wikitext
from app.services.runtime_config import load_runtime_config, runtime_config
from app.services.wikipedia_episode_parser import (
    has_animanga_film_infobox,
    has_tvanime_infobox,
)

APPLY_BATCH_SIZE = 20

logger = logging.getLogger("anime_backfill")

_TMDB_ID_RE = re.compile(r"^tmdb:(\d+)$")
_WIKI_HOST_RE = re.compile(r"^https?://([a-z-]+)\.wikipedia\.org/wiki/", re.IGNORECASE)


def _title_of(work) -> str:
    return work.title_cn or work.title_en or work.original_title or work.id


async def _select_undetermined(db) -> list[dict]:
    """All works with ``is_anime IS NULL`` as plain-dict snapshots.

    Scalar attributes are captured up front because the main loop calls
    ``session.rollback()``/``commit()`` (which expire every ORM object);
    accessing a lazy attribute afterwards raises MissingGreenlet.
    """
    rows: list[dict] = []
    for kind, model in (("series", TVSeries), ("movie", Movie)):
        result = await db.execute(
            select(model).where(model.is_anime.is_(None)).order_by(model.created_at)
        )
        for w in result.scalars().all():
            d = w.start_date if kind == "series" else w.release_date
            rows.append({
                "kind": kind,
                "id": w.id,
                "title": _title_of(w),
                "title_cn": w.title_cn,
                "title_en": w.title_en,
                "original_title": w.original_title,
                "aliases": [a for a in (w.aliases or []) if isinstance(a, str)],
                "year": d.year if d else None,
                "external_id": w.external_id,
                "external_source": w.external_source,
                "wikipedia_url": w.wikipedia_url,
            })
    return rows


def _tmdb_id_of(external_id: str | None, external_source: str | None) -> str | None:
    """Numeric TMDB id when the work's primary identity is TMDB, else None."""
    m = _TMDB_ID_RE.match(external_id or "")
    if m:
        return m.group(1)
    if (external_source or "").strip().lower() == "tmdb":
        candidate = (external_id or "").strip()
        if candidate.isdigit():
            return candidate
    return None


def _wiki_lang(url: str) -> str | None:
    """Wikipedia language segment from the URL host (zh/ja only)."""
    m = _WIKI_HOST_RE.match(url or "")
    if not m:
        return None
    lang = m.group(1).lower()
    return lang if lang in ("zh", "ja") else None


async def _bangumi_verdict_for(
    client: httpx.AsyncClient, work: dict, delay: float
) -> tuple[bool, str] | None:
    """Layer-1 Bangumi verification: search by the work's titles (no type
    filter — a 三次元 type-6 hit counts as live-action evidence) and apply
    the shared ``bangumi_verdict`` matcher (title + year guard)."""
    from app.services.anime_signals import bangumi_verdict
    from app.services.bangumi_client import search_subjects

    titles = [work["title_cn"], work["original_title"], work["title_en"], *work["aliases"]]
    for query in (work["title_cn"], work["original_title"], work["title_en"]):
        if not query:
            continue
        await asyncio.sleep(delay)
        subjects = await search_subjects(client, query)
        verdict, subj = bangumi_verdict(titles, work["year"], subjects)
        if verdict is not None:
            return verdict, f"bangumi#{subj.get('id')} type={subj.get('type')}"
    return None


async def _offline_verdict(
    db, kind: str, work_id: str, external_source: str | None
) -> tuple[bool, str] | None:
    """Anime-identity evidence from the primary source + identity bag."""
    bag = await list_external_ids(db, kind, work_id)
    alt_ids = [row.external_id for row in bag]
    if not is_anime_identity(external_source, alt_ids):
        return None
    src = (external_source or "").strip().lower()
    if src in ANIME_IDENTITY_SOURCES:
        return True, f"identity:{src} (primary)"
    hit = next(
        token.split(":", 1)[0].strip().lower()
        for token in alt_ids
        if token.split(":", 1)[0].strip().lower() in ANIME_IDENTITY_SOURCES
    )
    return True, f"identity:{hit} (bag)"


async def _tmdb_verdict(
    client: httpx.AsyncClient,
    cache: dict[tuple[str, str], bool | None],
    kind: str,
    tmdb_id: str,
    api_key: str,
) -> bool | None:
    """Tri-state verdict from TMDB details; results cached per (type, id)."""
    key = (kind, tmdb_id)
    if key in cache:
        return cache[key]
    media_type = "tv" if kind == "series" else "movie"
    resp = await client.get(
        f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}",
        params={"api_key": api_key},
    )
    resp.raise_for_status()
    data = resp.json()
    genres = data.get("genres") or []
    genre_ids = [g["id"] for g in genres if isinstance(g, dict) and "id" in g]
    verdict = is_anime_from_tmdb(
        genre_ids, data.get("original_language"), data.get("origin_country")
    )
    cache[key] = verdict
    return verdict


async def main(
    apply: bool,
    limit: int | None,
    delay: float,
    use_tmdb: bool,
    use_wikipedia: bool,
    use_bangumi: bool = False,
) -> None:
    phases = (
        "offline"
        + (" + bangumi" if use_bangumi else "")
        + (" + tmdb" if use_tmdb else "")
        + (" + wikipedia" if use_wikipedia else "")
    )
    print(f"=== is_anime backfill ({'APPLY' if apply else 'DRY-RUN'}, phases: {phases}) ===")

    tmdb_api_key = ""
    tmdb_client: httpx.AsyncClient | None = None
    bangumi_client: httpx.AsyncClient | None = None
    tmdb_cache: dict[tuple[str, str], bool | None] = {}
    counts = {"identity": 0, "bangumi": 0, "tmdb": 0, "wikipedia": 0,
              "undetermined": 0, "failed": 0}
    applied = 0

    session = async_session_factory()
    try:
        await load_runtime_config(session)
        rows = await _select_undetermined(session)
        print(f"{len(rows)} works with is_anime NULL (series/movie)")
        if limit:
            rows = rows[:limit]

        if use_bangumi:
            from app.services.bangumi_client import bangumi_configured

            if not bangumi_configured():
                print("BANGUMI_API_KEY is not set; skipping the bangumi phase.")
                use_bangumi = False
            else:
                bangumi_client = httpx.AsyncClient(timeout=15)

        if use_tmdb:
            tmdb_api_key = runtime_config.tmdb_api_key
            if not tmdb_api_key:
                print("TMDB_API_KEY is not set; skipping the tmdb phase.")
                use_tmdb = False
            else:
                tmdb_client = httpx.AsyncClient(timeout=15)

        for i, w in enumerate(rows, 1):
            kind, title = w["kind"], w["title"]
            verdict: bool | None = None
            evidence = ""
            try:
                # 1. offline identity evidence
                offline = await _offline_verdict(
                    session, kind, w["id"], w["external_source"]
                )
                if offline is not None:
                    verdict, evidence = offline
                    counts["identity"] += 1
                # 2. Bangumi search verification
                if verdict is None and use_bangumi:
                    hit = await _bangumi_verdict_for(bangumi_client, w, delay)
                    if hit is not None:
                        verdict, evidence = hit
                        counts["bangumi"] += 1
                # 3. TMDB details
                if verdict is None and use_tmdb:
                    tmdb_id = _tmdb_id_of(w["external_id"], w["external_source"])
                    if tmdb_id:
                        await asyncio.sleep(delay)
                        verdict = await _tmdb_verdict(
                            tmdb_client, tmdb_cache, kind, tmdb_id, tmdb_api_key
                        )
                        if verdict is not None:
                            counts["tmdb"] += 1
                            evidence = f"tmdb:{tmdb_id} (genres+language/country)"
                # 4. Wikipedia animanga infobox (TVAnime + film blocks)
                if verdict is None and use_wikipedia and w["wikipedia_url"]:
                    lang = _wiki_lang(w["wikipedia_url"])
                    if lang:
                        await asyncio.sleep(delay)
                        wikitext = await fetch_wikipedia_wikitext(w["wikipedia_url"], lang)
                        if has_tvanime_infobox(wikitext) or has_animanga_film_infobox(wikitext):
                            verdict = True
                            counts["wikipedia"] += 1
                            evidence = f"wikipedia:{lang} (animanga infobox)"
            except Exception as exc:  # one bad work must not kill the run
                counts["failed"] += 1
                logger.warning("[anime_backfill] %s %r failed: %s", kind, title, exc)
                await session.rollback()
                continue

            if verdict is None:
                counts["undetermined"] += 1
                print(f"[{i}/{len(rows)}] -- [{kind}] {title!r}: undetermined")
                continue
            print(f"[{i}/{len(rows)}] -> [{kind}] {title!r}: is_anime={verdict} ({evidence})")
            if apply:
                model = TVSeries if kind == "series" else Movie
                row = await session.get(model, w["id"])
                row.is_anime = verdict
                # Commit per work: the failure path's session.rollback()
                # discards ALL uncommitted changes, so batched commits would
                # silently lose earlier works' writes.
                await session.commit()
                applied += 1
                if applied % APPLY_BATCH_SIZE == 0:
                    print(f"    ... committed {applied} works")

        if apply:
            print(f"applied: updated {applied} works; committed.")
        else:
            print("dry-run: nothing written; re-run with --apply to execute.")
    finally:
        if tmdb_client is not None:
            await tmdb_client.aclose()
        if bangumi_client is not None:
            await bangumi_client.aclose()
        await session.close()

    print(
        f"\nsummary: identity={counts['identity']} bangumi={counts['bangumi']} "
        f"tmdb={counts['tmdb']} wikipedia={counts['wikipedia']} "
        f"undetermined={counts['undetermined']} failed={counts['failed']} of {len(rows)}"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--bangumi", action="store_true",
                        help="verify via Bangumi search (type 2 → anime, type 6 → live-action)")
    parser.add_argument("--tmdb", action="store_true",
                        help="query TMDB details for tmdb-identified works still undetermined")
    parser.add_argument("--wikipedia", action="store_true",
                        help="check wikipedia_url pages for an animanga infobox")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.25,
                        help="seconds between network calls (bangumi/tmdb/wikipedia phases)")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.limit, args.delay, args.tmdb, args.wikipedia,
                     args.bangumi))
