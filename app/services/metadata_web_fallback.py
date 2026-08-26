"""Web-search fallback (wigolo) for the metadata primary sources.

Pure leaf module - no DB, no LangGraph. When a primary source path returns
found=False (coverage gap, misclassified novel page, or bad translated title),
this module runs a web search on the self-hosted wigolo daemon (see
``wigolo_client``) and a single LLM judge call. The resulting
``matched_entity`` reuses the existing TVSeries/Movie upsert path with stable
IDs parsed from authoritative URLs (see ``metadata_source_registry`` for the
7-site identity scheme).

Candidate URLs are filtered to the channel's *ordered* fallback whitelist
(``Channel.metadata_fallback_sources``; empty list disables the fallback
entirely), and earlier-listed sources are preferred in evidence presentation.
The whitelist is additionally pushed down into wigolo via
``include_domains`` (subdomain matches), so the engines themselves stay inside
the identity sites; the post-filter remains as a hard guarantee.

Unlike the neural search it replaces, wigolo's keyword engines cannot digest
raw release titles ("[Group] 标题 S01E05 [1080p]"), so queries go through the
shared ``_candidate_queries`` cleaner (same variants the wikipedia/bangumi
sources search with): up to 3 cleaned work-name candidates are tried in order,
stopping at the first variant that yields hits.

The fallback supplies identity/links only: content (seasons / episode counts)
always follows the primary source, so any such fields the LLM emits are
stripped from the matched entity. The identity-less marker stays the legacy
literal ``"exa_web"`` — it is persisted on works and must remain stable.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import unquote, urlparse

from app.services.metadata_prompts import _WEB_FALLBACK_JUDGE_SYSTEM_PROMPT
from app.services.metadata_source_registry import (
    DEFAULT_FALLBACK_SOURCES,
    domains_for_sources,
)
from app.services.metadata_source_registry import (
    source_and_id_from_url as _registry_source_and_id_from_url,
)
from app.services.runtime_config import runtime_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL -> external_id/source mapping
# ---------------------------------------------------------------------------


def _source_and_id_from_url(url: str) -> tuple[str, str | None]:
    """Map an authoritative media-DB URL to (external_source, external_id).

    Returns ("exa_web", None) for unrecognised pages. This keeps the matched
    entity linkable by title even without a stable DB id. Percent-encoded URLs
    are decoded by the registry extractor before id extraction.
    """
    found = _registry_source_and_id_from_url(url)
    if found is None:
        return "exa_web", None
    return found


# ---------------------------------------------------------------------------
# wigolo search wrapper
# ---------------------------------------------------------------------------


async def _wigolo_web_search(
    query: str,
    include_domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Call the wigolo ``search`` tool and return normalised hits.

    Each hit has: url (decoded), title, text (truncated), source_domain,
    external_source, external_id (parsed from the URL where possible).
    """
    if not runtime_config.web_fallback_enabled:
        return []
    from app.services.wigolo_client import web_search

    try:
        raw_hits = await web_search(query, num_results=5, include_domains=include_domains)
    except Exception as e:
        logger.warning("[metadata_agent][web_fallback] search failed: %s", e)
        raise  # let caller classify as transient

    hits: list[dict[str, Any]] = []
    for r in raw_hits:
        # Keyword engines return percent-encoded URLs frequently; store the
        # decoded form so evidence, matched-entity URLs, and extracted ids all
        # converge with their plain-text shapes.
        url = unquote(r.get("url", "") or "")
        source, ext_id = _source_and_id_from_url(url)
        hits.append(
            {
                "url": url,
                "title": r.get("title", "") or "",
                "text": r.get("text", "") or "",
                "highlights": [],
                "source_domain": (urlparse(url).hostname or "").lower(),
                "external_source": source,
                "external_id": ext_id,
            }
        )
    return hits


def _default_searcher(include_domains: list[str]):
    """Bind the whitelist's domain push-down into the default searcher."""

    async def _search(query: str) -> list[dict[str, Any]]:
        return await _wigolo_web_search(query, include_domains=include_domains)

    return _search


# ---------------------------------------------------------------------------
# Evidence formatter + judge
# ---------------------------------------------------------------------------


def _build_evidence_text(hits: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, h in enumerate(hits[:5], 1):
        title = h.get("title", "") or ""
        url = h.get("url", "")
        source = h.get("external_source") or h.get("source_domain") or "web"
        ext_id = h.get("external_id")
        text = (h.get("text") or "")[:260].replace("\n", " ").strip()
        id_hint = f" (canonical id: {ext_id})" if ext_id else ""
        lines.append(f"[{i}] title={title}\n    source={source}{id_hint}\n    url={url}\n    text={text}")
    return "\n\n".join(lines)


async def _web_judge(
    model,
    raw_title: str,
    hits: list[dict[str, Any]],
    resource: Any | None = None,
) -> dict | None:
    """Run the web-fallback LLM judge. Returns a finalize dict or None.

    The judge is allowed to pick any of the provided web hits. The chosen hit's
    URL is parsed back into external_source/external_id by the caller.
    """
    if not hits:
        return None
    evidence_text = _build_evidence_text(hits)
    hints = ""
    if resource is not None:
        hints = (
            f"Pre-parsed hints: title_cn={getattr(resource, 'title_cn', None)!r} "
            f"title_en={getattr(resource, 'title_en', None)!r} "
            f"episode={getattr(resource, 'episode', None)} "
            f"season={getattr(resource, 'season', None)}"
        )
    user_msg = (
        f"RSS title: {raw_title}\n{hints}\n\nWeb search evidence:\n{evidence_text}\n\nReturn the finalize JSON now."
    )
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = await model.ainvoke(
            [
                SystemMessage(content=_WEB_FALLBACK_JUDGE_SYSTEM_PROMPT),
                HumanMessage(content=user_msg),
            ]
        )
    except Exception:
        logger.warning("[metadata_agent][web_fallback] judge LLM call failed for %r", raw_title[:80])
        return None
    content = getattr(resp, "content", "") or ""
    if isinstance(content, list):
        content = "".join(getattr(c, "text", str(c)) for c in content)
    # Reuse the same JSON extractor as the wikipedia judge path.
    from app.services.metadata_wiki_judge import _parse_finalize_json

    return _parse_finalize_json(content)


def _fallback_queries(raw_title: str, resource: Any | None) -> list[str]:
    """Cleaned work-name candidates for the keyword engine, capped at 3.

    Reuses the shared wikipedia/bangumi query cleaner; falls back to the raw
    title when cleaning yields nothing (e.g. an already-clean title that the
    non-work-name token filter rejects).
    """
    from app.services.metadata_wiki_query import _candidate_queries

    queries = [q for q, _lang in _candidate_queries(raw_title, resource)]
    queries = queries[:3]
    if not queries:
        queries = [raw_title]
    return queries


# ---------------------------------------------------------------------------
# Public fallback entry
# ---------------------------------------------------------------------------


async def web_fallback_judge(
    model,
    raw_title: str,
    resource: Any | None = None,
    *,
    web_searcher=None,
    fallback_sources: list[str] | None = None,
) -> tuple[dict, dict] | None:
    """Web-search fallback (wigolo) for a primary-source-not-found title.

    Kept name: three call sites + tests + cached method strings reference the
    fallback concept; the engine swap lives behind the searcher boundary.

    Returns a (finalize_dict, search_info) tuple:
      - found=True  -> match found on the web; search_info.error is None.
      - found=False -> searched and found no credible match; definitive.
      - search_info.error set -> the search itself failed (network/rate limit);
        the caller should treat this as transient and not cache.

    Returns None when the fallback is disabled/unconfigured OR the channel's
    fallback whitelist is empty (fallback disabled), letting the caller fall
    back to existing ReAct / not_found logic unchanged.

    ``fallback_sources`` is the channel's ordered whitelist of registry site
    names; None means the default order. Candidate URLs outside the whitelist
    are dropped (a hard filter), earlier-listed sources appear first in the
    judge's evidence (a preference signal), and the whitelist's domains are
    pushed down into wigolo's ``include_domains``. The matched entity carries
    identity only: content fields (seasons / episode counts) are stripped so
    content always follows the primary source.
    """
    whitelist = DEFAULT_FALLBACK_SOURCES if fallback_sources is None else list(fallback_sources)
    if not whitelist:
        return None

    if web_searcher is None:
        if not runtime_config.web_fallback_enabled:
            return None
        searcher = _default_searcher(domains_for_sources(whitelist))
    else:
        searcher = web_searcher

    try:
        merged: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        last_error: Exception | None = None
        for q in _fallback_queries(raw_title, resource):
            try:
                variant_hits = await searcher(q)
            except Exception as e:
                # A first-variant failure is transient infra; later-variant
                # failures after earlier successes are tolerable misses.
                last_error = e
                if not merged:
                    raise
                continue
            for h in variant_hits:
                url = h.get("url") or ""
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    merged.append(h)
            if merged:
                break  # keyword engines: first productive candidate wins
    except Exception as e:
        err = f"web search failed: {type(e).__name__}: {e}"[:200]
        logger.warning("[metadata_agent][web_fallback] %s for %r", err, raw_title[:80])
        return (
            {"found": False, "clean_title": raw_title, "content_type": "tv", "reason": err},
            {
                "method": "search_then_web_fallback",
                "data_sources_used": ["wigolo"],
                "source_errors": {"wigolo": err},
                "error": err,
            },
        )
    del last_error

    # Hard whitelist filter + ordered preference: keep only whitelisted
    # identity sites, earlier-listed sources first (stable sort).
    rank = {name: i for i, name in enumerate(whitelist)}
    hits = sorted(
        (h for h in merged if h.get("external_source") in rank),
        key=lambda h: rank[h["external_source"]],
    )

    if not hits:
        return (
            {
                "found": False,
                "clean_title": raw_title,
                "content_type": "tv",
                "reason": "no credible match in web search",
            },
            {"method": "search_then_web_fallback", "data_sources_used": ["wigolo"], "source_errors": {}, "error": None},
        )

    finalize_dict = await _web_judge(model, raw_title, hits, resource=resource)
    if finalize_dict is None:
        # Unparseable judge JSON - treat like a transient infra failure so the
        # resource is retried (we cannot safely declare a definitive not_found).
        err = "web fallback judge returned unparseable JSON"
        return (
            {"found": False, "clean_title": raw_title, "content_type": "tv", "reason": err},
            {
                "method": "search_then_web_fallback",
                "data_sources_used": ["wigolo"],
                "source_errors": {"wigolo": err},
                "error": err,
            },
        )

    # Ensure required defaults.
    finalize_dict.setdefault("clean_title", raw_title)
    finalize_dict.setdefault("content_type", "tv")

    # If the judge picked a matched_entity, parse its URL for a stable id if
    # one wasn't already supplied. The judge may set wikipedia_url or the URL
    # may live in the description; prefer the explicit external_id.
    me = finalize_dict.get("matched_entity") or {}
    if me:
        chosen_url = (
            me.get("wikipedia_url") or me.get("url") or _guess_url_from_description(me.get("description") or "")
        )
        if chosen_url:
            source, ext_id = _source_and_id_from_url(chosen_url)
            if not me.get("external_source"):
                me["external_source"] = source
            if not me.get("external_id"):
                me["external_id"] = ext_id
        # If the judge still produced no id, keep source="exa_web" and let the
        # title-based upsert handle convergence.
        if not me.get("external_source"):
            me["external_source"] = "exa_web"
        # Identity-only fallback: content (seasons / episode counts) follows
        # the primary source, so strip any such fields the LLM emitted.
        for key in ("seasons", "number_of_seasons", "number_of_episodes"):
            me.pop(key, None)
        finalize_dict["matched_entity"] = me

    return finalize_dict, {
        "method": "search_then_web_fallback",
        "data_sources_used": ["wigolo"],
        "source_errors": {},
        "error": None,
    }


def _guess_url_from_description(description: str) -> str:
    """Pull the first http URL out of a description fallback."""
    m = re.search(r"https?://[^\s\)<>\"]+", description or "")
    return m.group(0) if m else ""


__all__ = ["web_fallback_judge", "_wigolo_web_search", "_fallback_queries", "_source_and_id_from_url"]
