"""Search-first + single-LLM-judge path (S3) for the wikipedia source.

Extracted from ``metadata_agent`` (Phase 5). Runs candidate wikipedia searches
in parallel (no LLM), then ONE LLM call to judge the evidence and emit the
finalize JSON - cutting ReAct's ~4-5 LLM calls to 1.

``react_runner`` / ``msg_builder`` / ``model`` are injected (rather than read
off ``self``) so the agent's instance-attribute patches - e.g.
``agent._run_react = AsyncMock()`` - still intercept the ReAct fallback path
that lives here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.services.metadata_exa_fallback import exa_fallback_judge
from app.services.metadata_prompts import _JUDGE_SYSTEM_PROMPT
from app.services.metadata_sources import normalize_metadata_source_type
from app.services.metadata_wiki_classify import (
    _classify_wikipedia_page,
    _infer_content_type_from_categories,
)
from app.services.metadata_wiki_query import _candidate_queries
from app.services.metadata_wikipedia_client import (
    _execute_get_wikipedia_page,
    _execute_search_wikipedia,
)

logger = logging.getLogger(__name__)


def _parse_finalize_json(text: str) -> dict | None:
    """Extract the finalize JSON object from an LLM text response."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    candidate = fence.group(1) if fence else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(candidate[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


async def run_search_then_judge(
    model,
    raw_title: str,
    data_source_type: str | None = None,
    resource: Any | None = None,
    *,
    react_runner,
    msg_builder,
    exa_searcher=None,
) -> tuple[dict, dict]:
    """Search-first + single-LLM-judge path (S3) for the wikipedia source.

    Runs candidate wikipedia searches in parallel (no LLM), then ONE LLM
    call to judge the evidence and emit the finalize JSON - cutting the
    ~4-5 LLM calls of ReAct to 1. Falls back to ``_run_react`` when there
    is no usable query, the judge call fails, or its JSON is unparseable.

    When the wikipedia judge returns found=False, an optional Exa web-search
    fallback runs before the ReAct second opinion. Exa can close wikipedia's
    coverage gap (no page / misclassified novel page / bad translated title)
    by finding bangumi/TMDB/moegirl/Baidu Baike/etc. pages on the open web.
    """
    source = normalize_metadata_source_type(data_source_type)
    queries = _candidate_queries(raw_title, resource)
    if not queries:
        return await react_runner(
            msg_builder(raw_title, source), source
        )

    raw_results = await asyncio.gather(
        *(_execute_search_wikipedia(q, lang) for (q, lang) in queries),
        return_exceptions=True,
    )
    source_errors: dict[str, str] = {}
    # Collect top candidates (dedup by page_id) across variants.
    seen_pids: set = set()
    top: list[dict] = []
    for (q, lang), res in zip(queries, raw_results):
        if isinstance(res, Exception):
            source_errors[f"wikipedia:{lang}"] = f"{type(res).__name__}: {res}"[:200]
            continue
        if not isinstance(res, dict) or not res.get("success"):
            source_errors[f"wikipedia:{lang}"] = (
                res.get("error", "search failed")[:200]
                if isinstance(res, dict)
                else "no result"
            )
            continue
        for cand in res.get("data", [])[:3]:
            pid = cand.get("page_id")
            if pid and pid in seen_pids:
                continue
            if pid:
                seen_pids.add(pid)
            top.append({"query": q, "lang": lang, **cand})
            if len(top) >= 6:
                break
        if len(top) >= 6:
            break

    # Fetch full pages in parallel - categories are the strongest TV-vs-movie
    # signal and the search summary alone is often too thin for the judge to
    # confirm a match (this is what made ReAct's get_wikipedia_page step
    # worth its extra turn).
    page_results = await asyncio.gather(
        *(_execute_get_wikipedia_page(c["title"], c["lang"]) for c in top),
        return_exceptions=True,
    )
    evidence: list[dict] = []
    for cand, pres in zip(top, page_results):
        entry = dict(cand)
        if isinstance(pres, dict) and pres.get("data"):
            d = pres["data"]
            if d.get("disambiguation"):
                entry["disambiguation"] = True
            entry["categories"] = list(d.get("categories", [])[:10])
            if d.get("summary"):
                entry["summary"] = d["summary"][:400]
            if not entry.get("url") and d.get("url"):
                entry["url"] = d.get("url")
            if d.get("poster_url"):
                entry["poster_url"] = d["poster_url"]
        elif isinstance(pres, Exception):
            source_errors[f"page:{cand.get('lang')}"] = f"{type(pres).__name__}: {pres}"[:200]
        evidence.append(entry)

    # Deterministic auto-link: when a search result's title clearly matches
    # its query (similarity >= AUTO_LINK_THRESHOLD after OpenCC trad/simp
    # normalization), trust it without the LLM judge. The mini-LLM judge
    # often rejects obvious trad<->simp matches - e.g. simplified
    # "说出这边...传说" vs Wikipedia's traditional "說出這邊...傳說。" -
    # even though Wikipedia returned it as the top result.
    from app.services.metadata_service import AUTO_LINK_THRESHOLD
    from app.services.text_normalizer import similarity_score

    # Match each candidate's title against ALL candidate queries (not just
    # the one that first surfaced it). Page-id dedup above may associate a
    # page with a noisier long query even though a cleaner prefix query
    # also returned it - taking the max picks the clean match.
    all_query_strs = [q for q, _ in queries if q]
    best_auto: dict | None = None
    best_auto_score = 0
    best_auto_query = ""
    for e in evidence:
        if e.get("disambiguation"):
            continue
        title = e.get("title") or ""
        if not title or not all_query_strs:
            continue
        q = max(all_query_strs, key=lambda qq: similarity_score(qq, title))
        score = similarity_score(q, title)
        if score > best_auto_score:
            best_auto_score = score
            best_auto = e
            best_auto_query = q
    if best_auto is not None and best_auto_score >= AUTO_LINK_THRESHOLD:
        cats = best_auto.get("categories") or []
        page_kind = _classify_wikipedia_page(cats, best_auto.get("summary") or "")
        # B1: only auto-link a genuine creative work. A station / platform /
        # company / person / disambiguation page can title-match the query
        # (ViuTV, TVB, ...) well above the threshold - linking it on title
        # similarity alone is how the ViuTV television-station page became a
        # bogus series. non_work / ambiguous fall through to the LLM judge,
        # which can pick a different candidate or confirm no match.
        if page_kind == "work":
            ct = _infer_content_type_from_categories(cats)
            page_id = best_auto.get("page_id")
            lang = best_auto.get("lang")
            wiki_title = best_auto.get("title")
            finalize_dict = {
                "found": True,
                "clean_title": best_auto_query or wiki_title,
                "content_type": ct,
                "title_cn": wiki_title if lang == "zh" else None,
                "title_en": wiki_title if lang == "en" else None,
                "matched_entity": {
                    "external_id": f"wikipedia:{page_id}" if page_id else None,
                    "external_source": "wikipedia",
                    "title_cn": wiki_title if lang == "zh" else None,
                    "title_en": wiki_title if lang == "en" else None,
                    "description": (best_auto.get("summary") or "")[:500] or None,
                    "poster_url": best_auto.get("poster_url"),
                    "wikipedia_url": best_auto.get("url"),
                    "categories": list(cats[:10]),  # B3: carried for validation
                },
                "confidence": 0.9,
                "reason": f"auto-linked wikipedia result (title similarity {best_auto_score})",
            }
            logger.info(
                "[metadata_agent] auto-link %r -> %r (sim=%d, work, no judge)",
                raw_title[:80], wiki_title, best_auto_score,
            )
            return finalize_dict, {
                "method": "search_then_autolink",
                "data_sources_used": ["wikipedia"],
                "source_errors": source_errors,
                "error": None,
            }
        logger.info(
            "[metadata_agent] auto-link skipped for %r: top result %r is %s "
            "(sim=%d); deferring to judge",
            raw_title[:80], best_auto.get("title"), page_kind, best_auto_score,
        )
        # non_work / ambiguous -> fall through to the LLM judge below

    evidence_text = (
        "\n".join(
            f"[{i}] title={e.get('title')} page_id={e.get('page_id')} "
            f"url={e.get('url')} lang={e.get('lang')}\n    "
            f"categories={e.get('categories', [])[:6]}\n    "
            f"poster_url={e.get('poster_url')}\n    "
            f"summary={e.get('summary', '')[:280]}"
            for i, e in enumerate(evidence[:6], 1)
        )
        or "(no wikipedia results found for any variant)"
    )
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
        f"Wikipedia evidence:\n{evidence_text}\n\n"
        f"Return the finalize JSON now."
    )
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = await model.ainvoke(
            [SystemMessage(content=_JUDGE_SYSTEM_PROMPT), HumanMessage(content=user_msg)]
        )
    except Exception as e:
        logger.warning(
            "[metadata_agent] judge call failed for %r: %s; falling back to ReAct",
            raw_title[:80], e,
        )
        return await react_runner(
            msg_builder(raw_title, source), source
        )
    content = getattr(resp, "content", "") or ""
    if isinstance(content, list):  # some models return structured content
        content = "".join(getattr(c, "text", str(c)) for c in content)
    finalize_dict = _parse_finalize_json(content)
    if finalize_dict is None:
        logger.warning(
            "[metadata_agent] judge returned unparseable JSON for %r; falling back to ReAct",
            raw_title[:80],
        )
        return await react_runner(
            msg_builder(raw_title, source), source
        )
    # The single-call judge (especially on a mini model) can be conservative
    # and return found=False despite relevant evidence existing - a false
    # negative ReAct's multi-turn reasoning would catch. When that happens,
    # spend the extra ReAct run to verify; clear not-founds (no evidence at
    # all) are accepted as-is. Found=True results keep the fast 1-call path.
    if not finalize_dict.get("found"):
        exa_result = await exa_fallback_judge(
            model, raw_title, resource=resource, exa_searcher=exa_searcher,
        )
        if exa_result is not None:
            exa_finalize, exa_info = exa_result
            source_errors.update(exa_info.get("source_errors", {}))
            dsu = ["wikipedia", "exa"]
            if exa_info.get("error"):
                # Exa itself failed (network/rate/API) - transient. Do not run
                # ReAct; wikipedia already failed, and ReAct uses the same
                # wikipedia tools. Returning with the transient marker prevents
                # _classify_failure from caching this as not_found.
                logger.warning(
                    "[metadata_agent] wikipedia found=False and Exa failed for %r (%s); "
                    "treating as transient",
                    raw_title[:80], exa_info["error"],
                )
                return (
                    {
                        "found": False,
                        "clean_title": raw_title,
                        "content_type": finalize_dict.get("content_type", "tv"),
                        "reason": exa_info["error"],
                    },
                    {
                        "method": "search_then_exa_fallback",
                        "data_sources_used": dsu,
                        "source_errors": source_errors,
                        "error": exa_info["error"],
                    },
                )
            # Exa produced a definitive answer (found True or False). Return it
            # directly; skip the ReAct second opinion since Exa searched the
            # broader web and is the cheaper/broader fallback.
            logger.info(
                "[metadata_agent] wikipedia found=False, Exa fallback %s for %r",
                "found" if exa_finalize.get("found") else "not_found",
                raw_title[:80],
            )
            exa_finalize.setdefault("clean_title", raw_title)
            exa_finalize.setdefault("content_type", "tv")
            return exa_finalize, {
                "method": "search_then_exa_fallback",
                "data_sources_used": dsu,
                "source_errors": source_errors,
                "error": None,
            }
        # Exa not configured/disabled - fall through to the original ReAct logic.

    if not finalize_dict.get("found") and evidence:
        logger.info(
            "[metadata_agent] judge found=False with %d candidates for %r; ReAct second opinion",
            len(evidence), raw_title[:80],
        )
        return await react_runner(
            msg_builder(raw_title, source), source
        )
    # B3: carry the matched page's categories onto matched_entity so
    # process() can defense-check the entity kind. The judge returns
    # external_id "wikipedia:<page_id>"; find the evidence page with that
    # page_id and copy its categories (plus a description if missing).
    if finalize_dict.get("found"):
        me = finalize_dict.get("matched_entity") or {}
        ext_id = me.get("external_id") or ""
        pid = ext_id.split(":", 1)[1] if ext_id.startswith("wikipedia:") else None
        if pid:
            for e in evidence:
                if str(e.get("page_id")) == str(pid):
                    me["categories"] = list(e.get("categories", [])[:10])
                    if not me.get("description"):
                        me["description"] = (e.get("summary") or "")[:500] or None
                    break
            finalize_dict["matched_entity"] = me
    finalize_dict.setdefault("clean_title", "")
    finalize_dict.setdefault("content_type", "tv")
    logger.info(
        "[metadata_agent] judge done %r found=%s",
        raw_title[:80], finalize_dict.get("found"),
    )
    return finalize_dict, {
        "method": "search_then_judge",
        "data_sources_used": ["wikipedia"],
        "source_errors": source_errors,
        "error": None,
    }
