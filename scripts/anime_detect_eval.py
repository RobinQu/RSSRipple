"""Read-only evaluation of new is_anime detection signals (no DB writes).

Scans works whose ``is_anime`` is still NULL and runs four independent
detection methods against each, printing a per-work breakdown, per-method
counts, cross-method agreements and a conflict report. The script NEVER
writes to the database and has no --apply flag — it exists to measure each
signal's hit rate and false-positive risk before anything is wired into the
real backfill.

Methods:

- **M1 Bangumi search** (strongest evidence): ``POST /v0/search/subjects``
  with each candidate title (title_cn → original_title → title_en, first
  decisive hit stops). A candidate subject counts when its name/name_cn
  equals (after normalization) any work title/alias AND the year check
  passes (work year unknown, or |subject year - work year| <= 1; a missing
  subject date fails the check when the work year IS known). type==2 →
  anime, type==6 (三次元) → live-action, other types ignored. Needs
  ``BANGUMI_API_TOKEN`` in the environment (never hardcoded); without it the
  method is skipped.
- **M2 Wikipedia wikitext extension** (zh/ja wikipedia_url only): beyond the
  existing ``has_tvanime_infobox`` check, also detects
  ``{{Infobox animanga/(Movie|Film|OVA)}}`` blocks and anime-bearing bottom
  category links (categories containing 漫畫/漫画/comic are excluded so
  manga-adaptation pages don't false-positive).
- **M3 description regex** (pure offline, every NULL work): strict
  positive/negative pattern scan of the stored ``description``. Negative
  (真人 / live-action) wins over positive. This route exists to measure
  false-positive rate — patterns are deliberately narrow.
- **M4 TMDB keywords** (works with a numeric TMDB id only):
  ``GET /3/{tv|movie}/{id}/keywords`` — an "anime" keyword (TMDB keyword id
  210024, matched by name) → anime. Uses ``runtime_config.tmdb_api_key``;
  skipped when no key is configured.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Usage:
    uv run python scripts/anime_detect_eval.py [--limit N] [--delay S]
        [--kinds series|movie|both] [--include-determined]
        [--methods M1,M2,M3,M4] [--out report.md]
"""

import argparse
import asyncio
import os
import re

import httpx
from sqlalchemy import select

from app.database import async_session_factory
from app.models.movie import Movie
from app.models.series import TVSeries
from app.services.metadata_wikipedia_client import fetch_wikipedia_wikitext
from app.services.runtime_config import load_runtime_config, runtime_config
from app.services.text_normalizer import normalize_title
from app.services.wikipedia_episode_parser import has_tvanime_infobox

ANIME = "anime"
LIVE = "live-action"

BANGUMI_SEARCH_URL = "https://api.bgm.tv/v0/search/subjects"
BANGUMI_USER_AGENT = "robinqu/RSSRipple"

_WIKI_HOST_RE = re.compile(r"^https?://([a-z-]+)\.wikipedia\.org/wiki/", re.IGNORECASE)
_TMDB_ID_RE = re.compile(r"^tmdb:(\d+)$")

# M1 title equality: NFKC + OpenCC t2s + lowercase via normalize_title, then
# strip book-title marks (《》〈〉) and ALL whitespace.
_STRIP_RE = re.compile(r"[《》〈〉\s]+")

# M2 wikitext patterns.
_ANIMANGA_FILM_RE = re.compile(
    r"\{\{\s*Infobox\s+animanga/(?:Movie|Film|OVA)", re.IGNORECASE
)
_CATEGORY_RE = re.compile(r"\[\[(?:Category|分類|分类):([^\]]*)\]\]")
_CAT_ANIME_RE = re.compile(r"動畫|动画|アニメ|anime", re.IGNORECASE)
_CAT_COMIC_RE = re.compile(r"漫畫|漫画|comic", re.IGNORECASE)

# M3 description patterns (label, regex). Negative wins over positive.
_M3_POSITIVE: list[tuple[str, re.Pattern]] = [
    ("日本动画", re.compile(r"日本(?:電視動畫|動畫電影|动画)")),
    ("電視動畫", re.compile(r"電視動畫")),
    ("動畫電影", re.compile(r"動畫電影")),
    ("アニメ", re.compile(r"アニメ")),
    ("anime", re.compile(r"\banime\b", re.IGNORECASE)),
    ("Japanese animat", re.compile(r"Japanese animat", re.IGNORECASE)),
]
_M3_NEGATIVE: list[tuple[str, re.Pattern]] = [
    ("真人", re.compile(r"真人")),
    ("live-action", re.compile(r"live-action", re.IGNORECASE)),
]


def _norm(s: str | None) -> str:
    """Aggressive title-equality normalization (see _STRIP_RE)."""
    return _STRIP_RE.sub("", normalize_title(s))


def _title_of(work: dict) -> str:
    return work["title_cn"] or work["title_en"] or work["original_title"] or work["id"]


def _wiki_lang(url: str | None) -> str | None:
    """Wikipedia language segment from the URL host (zh/ja only)."""
    m = _WIKI_HOST_RE.match(url or "")
    if not m:
        return None
    lang = m.group(1).lower()
    return lang if lang in ("zh", "ja") else None


def _tmdb_id_of(work: dict) -> str | None:
    """Numeric TMDB id when the work's primary identity is TMDB, else None."""
    m = _TMDB_ID_RE.match(work["external_id"] or "")
    if m:
        return m.group(1)
    if (work["external_source"] or "").strip().lower() == "tmdb":
        external_id = (work["external_id"] or "").strip()
        if external_id.isdigit():
            return external_id
    return None


async def _select_works(
    db, kinds: str = "both", include_determined: bool = False
) -> list[dict]:
    """Snapshot works into plain dicts of scalar attrs.

    ``kinds``: "series" | "movie" | "both". ``include_determined``: when
    False (default), only is_anime-NULL works are returned; when True every
    work is included (its current ``is_anime`` is carried in the snapshot).

    Snapshotting at select time keeps the rest of the script free of ORM
    lazy-loading (MissingGreenlet) concerns — nothing here touches the
    session afterwards.
    """
    works: list[dict] = []
    for kind, model, date_attr in (
        ("series", TVSeries, "start_date"),
        ("movie", Movie, "release_date"),
    ):
        if kinds != "both" and kind != kinds:
            continue
        stmt = select(model).order_by(model.created_at)
        if not include_determined:
            stmt = stmt.where(model.is_anime.is_(None))
        result = await db.execute(stmt)
        for w in result.scalars().all():
            d = getattr(w, date_attr)
            works.append({
                "kind": kind,
                "id": w.id,
                "title_cn": w.title_cn,
                "title_en": w.title_en,
                "original_title": w.original_title,
                "aliases": [a for a in (w.aliases or []) if isinstance(a, str)],
                "external_id": w.external_id,
                "external_source": w.external_source,
                "wikipedia_url": w.wikipedia_url,
                "description": w.description,
                "year": d.year if d else None,
                "is_anime": w.is_anime,
            })
    return works


# ---------------------------------------------------------------------------
# M1 — Bangumi subject search
# ---------------------------------------------------------------------------


async def m1_bangumi(
    client: httpx.AsyncClient, work: dict, delay: float
) -> tuple[str | None, str, list[str]]:
    """Search Bangumi by candidate titles; first decisive subject wins.

    Returns (verdict, detail, trace) — the trace records every query and
    every returned subject's accept/reject reason, for manual inspection.
    """
    trace: list[str] = []
    work_titles = {
        _norm(t)
        for t in [work["title_cn"], work["original_title"], work["title_en"], *work["aliases"]]
    } - {""}
    if not work_titles:
        return None, "no titles", trace
    for query in (work["title_cn"], work["original_title"], work["title_en"]):
        if not query:
            continue
        await asyncio.sleep(delay)
        resp = await client.post(
            BANGUMI_SEARCH_URL, json={"keyword": query, "limit": 5}
        )
        resp.raise_for_status()
        subjects = (resp.json() or {}).get("data") or []
        trace.append(f"query {query!r} → {len(subjects)} hits")
        for subj in subjects:
            brief = (
                f"#{subj.get('id')} type={subj.get('type')} "
                f"name={subj.get('name')!r} name_cn={subj.get('name_cn')!r} "
                f"date={subj.get('date')} platform={subj.get('platform')}"
            )
            names = {_norm(subj.get("name")), _norm(subj.get("name_cn"))} - {""}
            if not (names & work_titles):
                trace.append(f"  reject {brief} — title mismatch")
                continue
            # Year guard: unknown work year passes; otherwise the subject
            # date must exist and be within +/-1 year.
            if work["year"] is not None:
                subj_date = str(subj.get("date") or "")
                subj_year = int(subj_date[:4]) if subj_date[:4].isdigit() else None
                if subj_year is None or abs(subj_year - work["year"]) > 1:
                    trace.append(f"  reject {brief} — year guard")
                    continue
            subj_type = subj.get("type")
            detail = (
                f"bangumi#{subj.get('id')} type={subj_type} q={query!r} "
                f"https://bangumi.tv/subject/{subj.get('id')}"
            )
            if subj_type == 2:
                trace.append(f"  HIT {brief} → anime")
                return ANIME, detail, trace
            if subj_type == 6:
                trace.append(f"  HIT {brief} → live-action")
                return LIVE, detail, trace
            # other types (book/game/music/...) — ignore, keep scanning
            trace.append(f"  skip {brief} — type not 2/6")
    return None, "", trace


# ---------------------------------------------------------------------------
# M2 — Wikipedia wikitext extension
# ---------------------------------------------------------------------------


async def m2_wikipedia(work: dict, delay: float) -> tuple[str | None, str]:
    """TVAnime / animanga-film infobox / anime category detection."""
    lang = _wiki_lang(work["wikipedia_url"])
    if not lang:
        return None, "no zh/ja wikipedia_url"
    await asyncio.sleep(delay)
    wikitext = await fetch_wikipedia_wikitext(work["wikipedia_url"], lang)
    if not wikitext:
        return None, "no wikitext"
    if has_tvanime_infobox(wikitext):
        return ANIME, "tvanime_infobox"
    m = _ANIMANGA_FILM_RE.search(wikitext)
    if m:
        return ANIME, f"animanga_film_infobox:{m.group(0).split('/')[-1].strip()}"
    for cat in _CATEGORY_RE.findall(wikitext):
        if _CAT_ANIME_RE.search(cat) and not _CAT_COMIC_RE.search(cat):
            return ANIME, f"category:{cat.strip()}"
    return None, ""


# ---------------------------------------------------------------------------
# M3 — description regex (offline)
# ---------------------------------------------------------------------------


def m3_description(work: dict) -> tuple[str | None, str]:
    """Strict positive/negative pattern scan of the stored description."""
    desc = work["description"] or ""
    if not desc:
        return None, "no description"
    for label, pattern in _M3_NEGATIVE:
        if pattern.search(desc):
            return LIVE, f"neg:{label}"
    for label, pattern in _M3_POSITIVE:
        if pattern.search(desc):
            return ANIME, f"pos:{label}"
    return None, ""


# ---------------------------------------------------------------------------
# M4 — TMDB keywords
# ---------------------------------------------------------------------------


async def m4_tmdb_keywords(
    client: httpx.AsyncClient, work: dict, delay: float, api_key: str
) -> tuple[str | None, str]:
    """TMDB keyword list containing "anime" (keyword id 210024) → anime."""
    tmdb_id = _tmdb_id_of(work)
    if not tmdb_id:
        return None, "no tmdb id"
    await asyncio.sleep(delay)
    media_type = "tv" if work["kind"] == "series" else "movie"
    resp = await client.get(
        f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/keywords",
        params={"api_key": api_key},
    )
    resp.raise_for_status()
    data = resp.json() or {}
    # TV returns {"results": [...]}, movies return {"keywords": [...]}.
    keywords = data.get("results") or data.get("keywords") or []
    for kw in keywords:
        if (kw.get("name") or "").strip().lower() == "anime":
            return ANIME, f"tmdb:{tmdb_id} keyword 'anime' (id {kw.get('id')})"
    return None, ""


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def main(
    limit: int | None,
    delay: float,
    kinds: str = "both",
    include_determined: bool = False,
    methods: tuple[str, ...] = ("M1", "M2", "M3", "M4"),
    out: str | None = None,
) -> None:
    print("=== is_anime detection eval (READ-ONLY) ===")
    async with async_session_factory() as db:
        await load_runtime_config(db)
        works = await _select_works(db, kinds, include_determined)
    scope = "all works" if include_determined else "works with is_anime NULL"
    print(f"{len(works)} {scope} (kinds={kinds}, methods={','.join(methods)})")
    if limit:
        works = works[:limit]

    # Prefer the one-off env override; otherwise the configured token
    # (settings UI / BANGUMI_API_KEY env) applies.
    bangumi_token = (
        os.environ.get("BANGUMI_API_TOKEN") or runtime_config.bangumi_api_key
    ).strip()
    if "M1" in methods and not bangumi_token:
        print("BANGUMI_API_TOKEN is not set; skipping M1 (bangumi search).")
    tmdb_api_key = runtime_config.tmdb_api_key
    if "M4" in methods and not tmdb_api_key:
        print("TMDB API key is not configured; skipping M4 (tmdb keywords).")

    # stats[method] = {"anime": [...titles], "live-action": [...titles], None: count}
    stats: dict[str, dict] = {
        m: {ANIME: [], LIVE: [], None: 0, "error": 0} for m in methods
    }
    verdicts: list[tuple[dict, dict[str, str | None], dict[str, str]]] = []
    traces: dict[str, list[str]] = {}  # work id -> M1 trace lines

    async with httpx.AsyncClient(
        timeout=20,
        headers={
            "User-Agent": BANGUMI_USER_AGENT,
            "Authorization": f"Bearer {bangumi_token}" if bangumi_token else "",
        },
    ) as client:
        for i, work in enumerate(works, 1):
            results: dict[str, str | None] = {}
            details: dict[str, str] = {}
            for name in methods:
                coro = None
                if name == "M1" and bangumi_token:
                    coro = m1_bangumi(client, work, delay)
                elif name == "M2":
                    coro = m2_wikipedia(work, delay)
                elif name == "M4" and tmdb_api_key:
                    coro = m4_tmdb_keywords(client, work, delay, tmdb_api_key)
                if name == "M3":
                    verdict, detail = m3_description(work)
                elif coro is None:
                    verdict, detail = None, "skipped"
                else:
                    try:
                        if name == "M1":
                            verdict, detail, trace = await coro
                            traces[work["id"]] = trace
                        else:
                            verdict, detail = await coro
                    except Exception as exc:  # one failure must not kill the run
                        stats[name]["error"] += 1
                        verdict, detail = None, f"ERROR: {exc}"
                results[name] = verdict
                details[name] = detail
                if verdict in (ANIME, LIVE):
                    stats[name][verdict].append(_title_of(work))
                elif detail != "skipped":
                    stats[name][None] += 1
            verdicts.append((work, results, details))
            cols = " ".join(
                f"{m}={results[m] or '-'}"
                + (f"({details[m]})" if results[m] else "")
                for m in methods
            )
            state = {True: "anime", False: "live", None: "NULL"}[work["is_anime"]]
            print(f"[{i}/{len(works)}] [{work['kind']}] {_title_of(work)!r} (is_anime={state}) | {cols}")

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n=== per-method summary ===")
    for m in methods:
        s = stats[m]
        print(
            f"{m}: anime={len(s[ANIME])} live-action={len(s[LIVE])} "
            f"no-verdict={s[None]} errors={s['error']}"
        )

    agreements: list[tuple[dict, dict[str, str | None]]] = []
    conflicts: list[tuple[dict, dict[str, str | None], dict[str, str]]] = []
    for work, results, details in verdicts:
        votes = {v for v in results.values() if v}
        if len(votes) == 1:
            agreeing = [m for m in methods if results[m]]
            if len(agreeing) >= 2:
                agreements.append((work, results))
        elif len(votes) > 1:
            conflicts.append((work, results, details))

    print(f"\n=== cross-method agreement (>=2 methods, same verdict): {len(agreements)} ===")
    for work, results in agreements:
        agreeing = {m: v for m, v in results.items() if v}
        print(f"  [{work['kind']}] {_title_of(work)!r}: {agreeing}")

    print(f"\n=== CONFLICTS (methods disagree): {len(conflicts)} ===")
    for work, results, details in conflicts:
        print(f"  [{work['kind']}] {_title_of(work)!r} (id={work['id']})")
        for m in methods:
            if results[m]:
                print(f"      {m}: {results[m]} — {details[m]}")

    print("\n=== per-method hit titles (for manual spot-check) ===")
    for m in methods:
        for verdict in (ANIME, LIVE):
            titles = stats[m][verdict]
            if titles:
                print(f"{m} {verdict} ({len(titles)}):")
                for t in titles:
                    print(f"  - {t}")

    # ── Detailed per-work report (Markdown) ─────────────────────────────
    if out:
        lines: list[str] = [
            "# is_anime 信号检测报告",
            "",
            f"- 范围: {scope}，kinds={kinds}，methods={', '.join(methods)}",
            f"- 作品数: {len(works)}",
            "",
            "| # | 作品 | 当前 is_anime | " + " | ".join(methods) + " |",
            "|---|---|---|" + "---|" * len(methods),
        ]
        for i, (work, results, details) in enumerate(verdicts, 1):
            state = {True: "anime", False: "live", None: "NULL"}[work["is_anime"]]
            cols = " | ".join(
                (results[m] or "—")
                + (f"<br>{details[m]}" if results[m] else "")
                for m in methods
            )
            lines.append(f"| {i} | {_title_of(work)} | {state} | {cols} |")
        lines.append("")
        for i, (work, results, details) in enumerate(verdicts, 1):
            state = {True: "anime", False: "live", None: "NULL"}[work["is_anime"]]
            lines.append(f"## {i}. [{work['kind']}] {_title_of(work)}")
            lines.append("")
            lines.append(f"- id: `{work['id']}`，year: {work['year']}，"
                         f"当前 is_anime: {state}")
            lines.append(
                f"- titles: cn={work['title_cn']!r} en={work['title_en']!r} "
                f"orig={work['original_title']!r}"
            )
            if work["aliases"]:
                lines.append(f"- aliases: {work['aliases']}")
            lines.append(
                f"- external: {work['external_source']}:{work['external_id']}，"
                f"wikipedia: {work['wikipedia_url'] or '—'}"
            )
            for m in methods:
                verdict = results[m] or "no-verdict"
                lines.append(f"- **{m}: {verdict}** {details[m]}")
            if work["id"] in traces:
                lines.append("- M1 trace:")
                lines.extend(f"  - {t}" for t in traces[work["id"]])
            lines.append("")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\nreport written to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.5,
                        help="seconds between network calls (bangumi/wikipedia/tmdb)")
    parser.add_argument("--kinds", choices=["series", "movie", "both"], default="both")
    parser.add_argument("--include-determined", action="store_true",
                        help="also scan works whose is_anime is already set")
    parser.add_argument("--methods", default="M1,M2,M3,M4",
                        help="comma-separated subset of M1,M2,M3,M4")
    parser.add_argument("--out", default=None,
                        help="write a detailed per-work Markdown report to this path")
    args = parser.parse_args()
    selected = tuple(m.strip() for m in args.methods.split(",") if m.strip())
    asyncio.run(main(
        args.limit, args.delay, args.kinds, args.include_determined, selected, args.out
    ))
