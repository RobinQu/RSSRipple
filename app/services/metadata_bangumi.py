"""Bangumi channel metadata source: search-first + deterministic auto-link +
single-LLM-judge (mirrors the wikipedia S3 path).

Search hits are matched against the cleaned candidate queries; exactly one
normalized-title-equal + year-guarded hit auto-links without the LLM.
Otherwise ONE judge call picks the subject (or declares no match). The
chosen subject is then expanded via the Bangumi details + episodes endpoints
into a matched_entity that drops into the existing upsert path:

  * ``external_source`` "bangumi" — the upsert's identity evidence already
    marks such works ``is_anime=True`` (the search is restricted to the
    anime category, type 2);
  * ``episode_list`` comes from ``/v0/episodes`` (main story, integer
    ``sort`` only); the season tag is the resource's parsed season marker
    (else 1 — a Bangumi subject IS one season);
  * TV subjects carry ``single_season_entry=True`` as match-scoped structural
    evidence. A Bangumi subject is one season, so the current resource can
    safely default to season 1 without writing a possibly false work-level
    ``number_of_seasons`` value. Movies do not carry this marker.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.services.anime_signals import bangumi_normalize_title
from app.services.bangumi_client import (
    bangumi_configured,
    get_subject,
    get_subject_episodes,
    search_subjects,
)
from app.services.metadata_prompts import _BANGUMI_JUDGE_SYSTEM_PROMPT
from app.services.metadata_wiki_judge import _parse_finalize_json
from app.services.metadata_wiki_query import _candidate_queries

logger = logging.getLogger(__name__)

_MAX_QUERIES = 6
_MAX_CANDIDATES = 8


def _bangumi_queries(raw_title: str, resource: Any | None) -> list[str]:
    """Reuse the wikipedia query builder (it already strips release metadata
    and season markers), keeping just the deduped query strings."""
    seen: set[str] = set()
    out: list[str] = []
    for q, _lang in _candidate_queries(raw_title, resource):
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:_MAX_QUERIES]


def _subject_names(subj: dict) -> set[str]:
    return {
        bangumi_normalize_title(subj.get("name")),
        bangumi_normalize_title(subj.get("name_cn")),
    } - {""}


def _autolink_subject(
    candidates: list[dict], queries: list[str], title_year: int | None
) -> dict | None:
    """Deterministic pick: exactly one candidate whose name/name_cn equals a
    query (normalized) and passes the title_year guard (±1 year)."""
    norm_queries = {bangumi_normalize_title(q) for q in queries} - {""}
    hits: list[dict] = []
    for subj in candidates:
        if not (_subject_names(subj) & norm_queries):
            continue
        if title_year is not None:
            d = str(subj.get("date") or "")
            sy = int(d[:4]) if d[:4].isdigit() else None
            if sy is None or abs(sy - title_year) > 1:
                continue
        hits.append(subj)
    return hits[0] if len(hits) == 1 else None


def _episode_list_from(episodes: list[dict], season: int) -> list[dict]:
    """Map Bangumi main-story episodes to our episode_list shape.

    ``sort`` is the global episode number; fractional sorts (in-between
    specials) and non-positive numbers are skipped.
    """
    out: list[dict] = []
    seen: set[int] = set()
    for ep in episodes:
        sort = ep.get("sort")
        if sort is None:
            continue
        num = int(sort)
        if num != sort or num <= 0:
            continue
        if num in seen:
            continue
        seen.add(num)
        out.append({
            "season": season,
            "episode": num,
            "title": ep.get("name_cn") or ep.get("name") or None,
        })
    out.sort(key=lambda e: e["episode"])
    return out


async def _build_matched_entity(
    client: httpx.AsyncClient, subject: dict, *, season: int
) -> dict:
    """Expand a search-hit subject into a matched_entity (details + full
    episode list). Best-effort: detail/episode failures degrade to the
    search hit's own fields."""
    sid = subject.get("id")
    detail: dict = {}
    try:
        detail = await get_subject(client, sid)
    except Exception as e:
        logger.warning("[metadata_bangumi] subject %s details failed: %s", sid, e)
    subj = {**subject, **{k: v for k, v in detail.items() if v is not None}}

    episodes: list[dict] = []
    try:
        episodes = await get_subject_episodes(client, sid)
    except Exception as e:
        logger.warning("[metadata_bangumi] subject %s episodes failed: %s", sid, e)
    ep_list = _episode_list_from(episodes, season)

    images = subj.get("images") or {}
    tags = [t.get("name") for t in (subj.get("tags") or [])[:8] if t.get("name")]
    platform = str(subj.get("platform") or "")
    content_type = "movie" if platform == "剧场版" else "tv"
    entity = {
        "external_id": f"bangumi:{sid}",
        "external_source": "bangumi",
        "title_cn": subj.get("name_cn") or subj.get("name"),
        "original_title": subj.get("name"),
        "description": (subj.get("summary") or "")[:2000] or None,
        "poster_url": images.get("large") or images.get("common"),
        "rating": (subj.get("rating") or {}).get("score"),
        "start_date": subj.get("date") or None,
        "number_of_episodes": subj.get("eps") or (len(ep_list) or None),
        "genre": tags,
        "is_anime": True,  # the search is restricted to the anime category
        "episode_list": ep_list or None,
        "_content_type": content_type,
    }
    if content_type == "tv":
        entity["single_season_entry"] = True
    return entity


def _build_evidence_text(candidates: list[dict]) -> str:
    lines: list[str] = []
    for i, s in enumerate(candidates[:_MAX_CANDIDATES], 1):
        tags = ", ".join(
            t.get("name", "") for t in (s.get("tags") or [])[:5] if t.get("name")
        )
        summary = (s.get("summary") or "")[:260].replace("\n", " ").strip()
        lines.append(
            f"[{i}] id={s.get('id')} name={s.get('name')!r} "
            f"name_cn={s.get('name_cn')!r} date={s.get('date')} "
            f"platform={s.get('platform')}\n"
            f"    tags={tags}\n"
            f"    summary={summary}"
        )
    return "\n\n".join(lines)


async def _judge(
    model, raw_title: str, candidates: list[dict], resource: Any | None
) -> dict | None:
    """Single LLM call to pick the matching subject. Returns the finalize
    dict or None (unparseable / call failed)."""
    hints = ""
    if resource is not None:
        hints = (
            f"Pre-parsed hints: title_cn={getattr(resource, 'title_cn', None)!r} "
            f"title_en={getattr(resource, 'title_en', None)!r} "
            f"episode={getattr(resource, 'episode', None)} "
            f"season={getattr(resource, 'season', None)}"
        )
    user_msg = (
        f"RSS title: {raw_title}\n{hints}\n\n"
        f"Bangumi subjects:\n{_build_evidence_text(candidates)}\n\n"
        f"Return the finalize JSON now."
    )
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = await model.ainvoke(
            [SystemMessage(content=_BANGUMI_JUDGE_SYSTEM_PROMPT),
             HumanMessage(content=user_msg)]
        )
    except Exception:
        logger.warning(
            "[metadata_bangumi] judge LLM call failed for %r", raw_title[:80]
        )
        return None
    content = getattr(resp, "content", "") or ""
    if isinstance(content, list):
        content = "".join(getattr(c, "text", str(c)) for c in content)
    return _parse_finalize_json(content)


async def run_bangumi_search_then_judge(
    model,
    raw_title: str,
    resource: Any | None = None,
) -> tuple[dict, dict]:
    """Bangumi source entry: search → auto-link or judge → matched_entity.

    Returns (finalize_dict, search_info). Never raises for source-side
    failures — they surface via ``search_info["error"]`` (infra failures
    classify as transient downstream).
    """
    search_info: dict[str, Any] = {
        "method": "bangumi",
        "data_sources_used": ["bangumi"],
        "source_errors": {},
        "error": None,
    }

    def _miss(reason: str, error: str | None = None) -> tuple[dict, dict]:
        search_info["error"] = error
        return (
            {"found": False, "clean_title": raw_title, "content_type": "tv",
             "reason": reason},
            search_info,
        )

    if not bangumi_configured():
        return _miss(
            "Bangumi API key not configured",
            "Bangumi: api key not configured",
        )

    queries = _bangumi_queries(raw_title, resource)
    if not queries:
        return _miss("no usable query")

    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Search all candidate queries, merge + dedup by subject id.
        raw_results = await asyncio.gather(
            *(search_subjects(client, q, limit=5, anime_only=True) for q in queries),
            return_exceptions=True,
        )
        seen_ids: set = set()
        candidates: list[dict] = []
        for q, res in zip(queries, raw_results):
            if isinstance(res, Exception):
                err = f"{type(res).__name__}: {res}"[:200]
                search_info["source_errors"]["bangumi"] = err
                search_info["error"] = search_info["error"] or f"Bangumi: {err}"
                continue
            for subj in res:
                sid = subj.get("id")
                if sid is None or sid in seen_ids:
                    continue
                seen_ids.add(sid)
                candidates.append(subj)
                if len(candidates) >= _MAX_CANDIDATES:
                    break
            if len(candidates) >= _MAX_CANDIDATES:
                break

        if not candidates:
            return _miss("no credible match on Bangumi", search_info["error"])

        title_year = getattr(resource, "title_year", None) if resource else None
        resource_season = getattr(resource, "season", None) if resource else None

        # 2. Deterministic auto-link (no LLM) on an unambiguous exact match.
        picked = _autolink_subject(candidates, queries, title_year)
        method = "bangumi_search_then_autolink"
        judge_dict: dict | None = None
        if picked is None:
            # 3. Single judge call over the candidate set.
            judge_dict = await _judge(model, raw_title, candidates, resource)
            if judge_dict is None:
                return _miss(
                    "bangumi judge returned unparseable JSON",
                    "Bangumi: judge call failed",
                )
            method = "bangumi_search_then_judge"
            if not judge_dict.get("found"):
                search_info["method"] = method
                judge_dict.setdefault("clean_title", raw_title)
                judge_dict.setdefault("content_type", "tv")
                return judge_dict, search_info
            me = judge_dict.get("matched_entity") or {}
            ext_id = str(me.get("external_id") or "")
            sid = ext_id.split(":", 1)[1] if ext_id.startswith("bangumi:") else None
            picked = next(
                (s for s in candidates if str(s.get("id")) == str(sid)), None
            )
            if picked is None:
                return _miss("bangumi judge picked an unknown subject")

        # 4. Expand the chosen subject into a matched_entity.
        season = resource_season or (judge_dict or {}).get("inferred_season") or 1
        entity = await _build_matched_entity(client, picked, season=season)
        content_type = entity.pop("_content_type")

        finalize_dict: dict[str, Any] = {
            "found": True,
            "clean_title": (
                (judge_dict or {}).get("clean_title")
                or (resource.search_title if resource is not None else None)
                or raw_title
            ),
            "content_type": content_type,
            "title_cn": entity.get("title_cn"),
            "matched_entity": entity,
            "confidence": 0.9 if method.endswith("autolink") else 0.8,
            "reason": (
                "auto-linked bangumi subject (normalized title match)"
                if method.endswith("autolink")
                else "bangumi judge pick"
            ),
        }
        # Carry the judge's episode/season inference through.
        for key in (
            "inferred_episode", "inferred_season", "is_batch",
            "inferred_episode_start", "inferred_episode_end",
            "subtitle_groups", "subtitle_group", "resolution", "title_en",
        ):
            value = (judge_dict or {}).get(key)
            if value is not None:
                finalize_dict[key] = value
        search_info["method"] = method
        logger.info(
            "[metadata_bangumi] %r -> bangumi:%s (%s)",
            raw_title[:80], picked.get("id"), method,
        )
        return finalize_dict, search_info
