"""Regression harness for the wigolo web-search fallback (production shape).

Searcher variants are wrapped as ``web_searcher``-compatible async callables
(same hit shape as ``metadata_web_fallback._wigolo_web_search``) and injected
into the PRODUCTION pipeline ``web_fallback_judge``, so every run exercises the
real query build / whitelist filter / evidence formatting / LLM judge path.
The production searcher itself is ``wigolo-clean``'s logic; the other variants
exist to quantify why each adaptation (domain push-down, candidate cleaning)
is required.

Backends:
  * wigolo        — POST {WIGOLO_BASE_URL}/v1/search, unconstrained raw title.
  * wigolo-domains— raw title + ``include_domains`` whitelist push-down.
  * wigolo-clean  — domains + multi-candidate queries via the shared
                    ``_candidate_queries`` cleaner (the production shape).

Usage:
    python scripts/wigolo_fallback_eval.py [--no-judge] [--limit N]
        [--backends wigolo,wigolo-domains,wigolo-clean] [--json-out PATH]

Env:
    WIGOLO_BASE_URL   default http://flash-aio:3333
    WIGOLO_API_TOKEN  bearer token for non-loopbind daemons
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.services.metadata_source_registry import DEFAULT_FALLBACK_SOURCES  # noqa: E402
from app.services.metadata_web_fallback import (  # noqa: E402
    _source_and_id_from_url,
    web_fallback_judge,
)

WIGOLO_BASE_URL = os.environ.get("WIGOLO_BASE_URL", "http://flash-aio:3333").rstrip("/")
WIGOLO_API_TOKEN = os.environ.get("WIGOLO_API_TOKEN", "")

# Registry site -> domains passed to wigolo include_domains.
SITE_DOMAINS: dict[str, list[str]] = {
    "bangumi": ["bangumi.tv", "bgm.tv"],
    "mal": ["myanimelist.net"],
    "anilist": ["anilist.co"],
    "tmdb": ["themoviedb.org"],
    "wikipedia": ["wikipedia.org"],
    "imdb": ["imdb.com"],
    "douban": ["douban.com"],
}

# Historical RSS titles harvested from the test suite + realistic additions.
# expect_found=False rows assert the definitive not-found path stays intact.
CORPUS: list[dict] = [
    {
        "raw_title": "[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 03 [WebRip 1080p]",
        "expect_found": True,
        "note": "zh title + EN alt, season marker",
    },
    {
        "raw_title": "[ANi] 关于我转生变成史莱姆这档事 第四季 - 83 [1080P][Baha]",
        "expect_found": True,
        "note": "absolute episode number, zh-only",
    },
    {
        "raw_title": "[ANi] 葬送的芙莉莲 - 01 [1080p AVC AAC][Baha]",
        "expect_found": True,
        "note": "zh-only, no season marker",
    },
    {
        "raw_title": "[H-Enc] 葬送的芙莉莲 第二季 / Sousou no Frieren 2nd Season (BDRip 1080p HEVC FLAC)",
        "expect_found": True,
        "note": "batch pack, S2 marker",
    },
    {
        "raw_title": "[NEST] 黄泉使者 / 黄泉のツガイ / Daemons of the Shadow Realm - 12 [CR WEB-DL 1080p AVC AAC]",
        "expect_found": True,
        "note": "zh/ja/en triple title",
    },
    {
        "raw_title": "魔法少女まどか☆マギカ / Madoka Magica - 01 [1080p]",
        "expect_found": True,
        "note": "ja-led title with star glyph",
    },
    {
        "raw_title": "[Nix-Raws] Mushoku Tensei S03E06 [CR WEB-DL 1080p]",
        "expect_found": True,
        "note": "EN-only, SxxEyy",
    },
    {
        "raw_title": "[SubsPlease] Kusuriya no Hitorigoto - 30 (1080p) [EDD1B716].mkv",
        "expect_found": True,
        "note": "romaji-only title",
    },
    {
        "raw_title": "すずめの戸締まり [劇場版] WebDL 2160p HEVC",
        "expect_found": True,
        "content_type_hint": "movie",
        "note": "ja movie",
    },
    {
        "raw_title": "沙丘2 / Dune Part Two 2024 2160p REMUX",
        "expect_found": True,
        "content_type_hint": "movie",
        "note": "en/zh movie",
    },
    {
        "raw_title": "黑暗荣耀 第二季 / The Glory S02 1080p NF WEB-DL",
        "expect_found": True,
        "note": "live-action kdrama (non-anime)",
    },
    {
        "raw_title": "[整理搬运] 新世纪福音战士 (EVANGELION)：TV动画+剧场版",
        "expect_found": True,
        "note": "franchise pack",
    },
    {
        "raw_title": "[FAKE组] 完全不存在虚构动画作品第二季 - 01 [1080p][v2]",
        "expect_found": False,
        "note": "nonexistent work -> definitive not-found",
    },
]


# ---------------------------------------------------------------------------
# Searchers producing the exact production hit shape
# ---------------------------------------------------------------------------


def _normalize_hit(r: dict) -> dict:
    url = r.get("url", "") or ""
    source, ext_id = _source_and_id_from_url(url)
    return {
        "url": url,
        "title": r.get("title", "") or "",
        "text": (r.get("snippet") or r.get("text") or "")[:600],
        "highlights": [],
        "source_domain": (urlparse(url).hostname or "").lower(),
        "external_source": source,
        "external_id": ext_id,
    }


def _wigolo_headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if WIGOLO_API_TOKEN:
        h["Authorization"] = f"Bearer {WIGOLO_API_TOKEN}"
    return h


async def make_wigolo_searcher(use_domains: bool, use_candidates: bool = False):
    """Build an async searcher(query)->hits around POST /v1/search.

    With ``use_candidates`` the incoming query is ignored in favour of the
    shared ``_candidate_queries`` variants (deduped by URL across variants,
    early-exit once a variant yields hits) — the keyword-engine adaptation.
    """

    async def _search(query: str, num_results: int = 5) -> list[dict]:
        body_base: dict = {
            "max_results": max(num_results, 8),  # over-fetch; whitelist filters later
            "search_depth": "balanced",
        }
        if use_domains:
            doms: list[str] = []
            for site in DEFAULT_FALLBACK_SOURCES:
                doms.extend(SITE_DOMAINS[site])
            body_base["include_domains"] = doms
        queries = [query]
        if use_candidates:
            from app.services.metadata_wiki_query import _candidate_queries

            cands = [q for q, _lang in _candidate_queries(query, None)]
            if cands:
                queries = cands
        merged: list[dict] = []
        seen_urls: set[str] = set()
        async with httpx.AsyncClient(timeout=90.0) as client:
            for q in queries[:3]:
                try:
                    resp = await client.post(
                        f"{WIGOLO_BASE_URL}/v1/search",
                        json={
                            **body_base,
                            "query": q,
                        },
                        headers=_wigolo_headers(),
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception:
                    if not merged:
                        raise  # first variant failing = transient, like production
                    continue
                warn = data.get("engine_warnings") or []
                if warn:
                    print(f"    [wigolo engine_warnings] {q!r}: {[w.get('engine') for w in warn]}")
                for r in data.get("results", []):
                    hit = _normalize_hit(r)
                    if hit["url"] and hit["url"] not in seen_urls:
                        seen_urls.add(hit["url"])
                        merged.append(hit)
                if merged:
                    break  # keyword engines: first productive variant wins
        return merged

    return _search


# ---------------------------------------------------------------------------
# Offline contract checks (no network)
# ---------------------------------------------------------------------------


def offline_checks() -> bool:
    print("== Offline contract checks ==")
    ok = True
    cases = [
        ("https://bangumi.tv/subject/501963", ("bangumi", "bangumi:501963")),
        ("https://bgm.tv/subject/7", ("bangumi", "bangumi:7")),
        (
            "https://en.wikipedia.org/wiki/Mushoku_Tensei_(TV_series)",
            ("wikipedia", "wikipedia:Mushoku_Tensei_(TV_series)"),
        ),
        ("https://zh.wikipedia.org/wiki/葬送的芙莉莲", ("wikipedia", "wikipedia:葬送的芙莉莲")),
        ("https://www.themoviedb.org/tv/94664", ("tmdb", "tmdb:94664")),
        ("https://www.themoviedb.org/movie/533535", ("tmdb", "tmdb:533535")),
        ("https://myanimelist.net/anime/5114", ("mal", "mal:5114")),
        ("https://anilist.co/anime/21", ("anilist", "anilist:21")),
        ("https://www.imdb.com/title/tt13616990/", ("imdb", "imdb:tt13616990")),
        ("https://movie.douban.com/subject/35701847/", ("douban", "douban:35701847")),
    ]
    for url, expected in cases:
        got = _source_and_id_from_url(url)
        flag = "OK " if got == expected else "FAIL"
        ok &= got == expected
        print(f"  [{flag}] {url}\n         -> {got}")
    # noise domains must NOT be granted identity
    for url in [
        "https://nyaa.si/view/2131889",
        "https://www.dmhy.org/topics/view/1.html",
        "https://baike.baidu.com/item/x/123",
    ]:
        src, ext_id = _source_and_id_from_url(url)
        good = src == "exa_web" and ext_id is None
        ok &= good
        print(f"  [{'OK ' if good else 'FAIL'}] noise domain stays identity-less: {src} ({url})")
    return ok


# ---------------------------------------------------------------------------
# Pipeline evaluation
# ---------------------------------------------------------------------------


def _resource_stub(item: dict):
    return SimpleNamespace(
        title_cn=None,
        title_en=None,
        episode=None,
        season=None,
    )


async def eval_backend(name: str, searcher, items: list[dict], judge: bool, results: dict, limit: int | None) -> None:
    model = None
    if judge:
        from langchain_openai import ChatOpenAI

        from app.services.runtime_config import runtime_config

        model = ChatOpenAI(
            model=runtime_config.llm_model,
            api_key=runtime_config.llm_api_key,
            base_url=runtime_config.llm_base_url,
            temperature=0.1,
            timeout=60,
            max_retries=2,
        )

    rows = []
    selected = items if limit is None else items[:limit]
    for item in selected:
        raw_title = item["raw_title"]
        print(f"\n--- [{name}] {raw_title[:76]}")
        t0 = time.monotonic()
        finalize, info = await web_fallback_judge(
            model or SimpleNamespace(),
            raw_title,
            resource=_resource_stub(item),
            web_searcher=searcher,
        )
        wall = time.monotonic() - t0
        found = bool(finalize and finalize.get("found"))
        me = (finalize or {}).get("matched_entity") or {}
        row = {
            "backend": name,
            "raw_title": raw_title,
            "note": item["note"],
            "expect_found": item["expect_found"],
            "found": found,
            "reason": (finalize or {}).get("reason"),
            "error": info.get("error"),
            "clean_title": (finalize or {}).get("clean_title"),
            "entity_source": me.get("external_source"),
            "entity_id": me.get("external_id"),
            "entity_url": me.get("url") or me.get("wikipedia_url"),
            "confidence": (finalize or {}).get("confidence"),
            "wall_seconds": round(wall, 1),
            "method": info.get("method"),
        }
        verdict = "OK " if found == item["expect_found"] else "MISS"
        print(
            f"    found={found} expected={item['expect_found']} [{verdict}] "
            f"entity={row['entity_source']}:{row['entity_id']} "
            f"clean={row['clean_title']!r} wall={row['wall_seconds']}s"
        )
        if not found and row["reason"]:
            print(f"      reason: {str(row['reason'])[:120]}")
        rows.append(row)

    hits_ok = sum(1 for r in rows if r["found"] == r["expect_found"])
    print(f"\n[{name}] agreement: {hits_ok}/{len(rows)}")
    results[name] = rows


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--no-judge", action="store_true", help="skip the LLM judge; search+filter only (finalize comes back not-found)"
    )
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--backends", type=str, default="wigolo,wigolo-domains,exa")
    ap.add_argument("--json-out", type=str, default="/tmp/opencode/wigolo_eval_results.json")
    args = ap.parse_args()

    if not offline_checks():
        print("offline checks FAILED; aborting")
        sys.exit(1)

    async def build_searcher(name: str):
        if name == "wigolo":
            return await make_wigolo_searcher(False)
        if name == "wigolo-domains":
            return await make_wigolo_searcher(True)
        return await make_wigolo_searcher(True, use_candidates=True)

    wanted = [b.strip() for b in args.backends.split(",") if b.strip()]
    known = ("wigolo", "wigolo-domains", "wigolo-clean")
    unknown = [b for b in wanted if b not in known]
    if unknown:
        print(f"unknown backends: {unknown}; choose from {known}")
        sys.exit(1)

    results: dict[str, list[dict]] = {}
    for name in wanted:
        searcher = await build_searcher(name)
        try:
            await eval_backend(name, searcher, CORPUS, not args.no_judge, results, args.limit)
        except Exception as e:
            print(f"[{name}] backend FAILED: {type(e).__name__}: {e}")

    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    print("\n================ SUMMARY ================")
    for name, rows in results.items():
        agree = sum(1 for r in rows if r["found"] == r["expect_found"])
        ident = sum(1 for r in rows if r["found"] and r["entity_id"])
        avg = sum(r["wall_seconds"] for r in rows) / max(len(rows), 1)
        pref = sum(1 for r in rows if r["found"] and r["entity_source"] in ("bangumi", "mal", "anilist"))
        print(
            f"{name:16s} agree={agree}/{len(rows)}  "
            f"identity-parsed={ident}/{sum(1 for r in rows if r['found'])}  "
            f"anime-db-first={pref}  avg_wall={avg:.1f}s"
        )
    print(f"\nJSON -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
