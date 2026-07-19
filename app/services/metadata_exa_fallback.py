"""Exa web-search fallback for the wikipedia metadata source.

Pure leaf module - no DB, no LangGraph. When the Wikipedia S3 path returns
found=False (coverage gap, misclassified novel page, or bad translated title),
this module runs one Exa web search and a single LLM judge call. The resulting
``matched_entity`` reuses the existing TVSeries/Movie upsert path with stable
IDs parsed from authoritative URLs (bangumi, tmdb, mal, anilist, wikipedia,
baike, eiga, ...).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from app.services.metadata_prompts import _EXA_JUDGE_SYSTEM_PROMPT
from app.services.runtime_config import runtime_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL -> external_id/source mapping
# ---------------------------------------------------------------------------

# (source_domain_or_host_suffix, regex_with_named_group, external_source)
_URL_EXTRACTORS: list[tuple[str, re.Pattern, str]] = [
    (r"bangumi\.tv|bgm\.tv", re.compile(r"/subject/(?P<id>\d+)"), "bangumi"),
    (r"themoviedb\.org", re.compile(r"/tv/(?P<id>\d+)"), "tmdb"),
    (r"myanimelist\.net", re.compile(r"/anime/(?P<id>\d+)"), "mal"),
    (r"anilist\.co", re.compile(r"/anime/(?P<id>\d+)"), "anilist"),
    (r"imdb\.com", re.compile(r"/(?:title/)?(?P<id>tt\d+)"), "imdb"),
    (r"baike\.baidu\.com", re.compile(r"/item/(?P<id>[^/?#]+)"), "baidu_baike"),
    (r"movie\.douban\.com", re.compile(r"/subject/(?P<id>\d+)"), "douban"),
    (r"eiga\.com", re.compile(r"/(?:news|movie|cinema)/(\d+)?"), "eiga"),
]


def _source_and_id_from_url(url: str) -> tuple[str, str | None]:
    """Map an authoritative media-DB URL to (external_source, external_id).

    Returns ("exa_web", None) for unrecognised pages. This keeps the matched
    entity linkable by title even without a stable DB id.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    for host_pat, id_re, source in _URL_EXTRACTORS:
        if not re.search(host_pat, host):
            continue
        m = id_re.search(path)
        if not m:
            continue
        raw_id = m.group("id") if "id" in m.groupdict() else m.group(1)
        # URL-decode Baidu Baike slugs so the same work converges across encodings.
        if source == "baidu_baike":
            from urllib.parse import unquote
            raw_id = unquote(raw_id).split("/")[0].strip()
            if not raw_id:
                continue
        if source == "eiga":
            # eiga news URLs don't embed a stable numeric id; fall back to the
            # domain as a source with no canonical id.
            if not raw_id:
                return source, None
        return source, f"{source}:{raw_id}"
    return "exa_web", None


# ---------------------------------------------------------------------------
# Exa search wrapper
# ---------------------------------------------------------------------------

async def _exa_web_search(query: str) -> list[dict[str, Any]]:
    """Call Exa /search for a query and return normalised web hits.

    Each hit has: url, title, text (truncated), source_domain, external_source,
    external_id (parsed from the URL where possible).
    """
    if not runtime_config.exa_api_key or not runtime_config.exa_enabled:
        return []
    from exa_py import AsyncExa

    exa = AsyncExa(api_key=runtime_config.exa_api_key)
    try:
        resp = await exa.search(
            query=query,
            type="neural",
            num_results=5,
            contents={"text": True, "highlights": True},
        )
    except Exception as e:
        logger.warning("[metadata_agent][exa_fallback] search failed: %s", e)
        raise  # let caller classify as transient

    hits: list[dict[str, Any]] = []
    for r in getattr(resp, "results", []) or []:
        def g(name: str) -> str:
            if isinstance(r, dict):
                return r.get(name, "") or ""
            return getattr(r, name, "") or ""

        url = g("url")
        source, ext_id = _source_and_id_from_url(url)
        hits.append({
            "url": url,
            "title": g("title"),
            "text": g("text"),
            "highlights": list(g("highlights") or [])[:2],
            "source_domain": (urlparse(url).hostname or "").lower(),
            "external_source": source,
            "external_id": ext_id,
        })
    return hits


# ---------------------------------------------------------------------------
# Evidence formatter + judge
# ---------------------------------------------------------------------------

def _build_exa_evidence_text(hits: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, h in enumerate(hits[:5], 1):
        title = h.get("title", "") or ""
        url = h.get("url", "")
        source = h.get("external_source") or h.get("source_domain") or "web"
        ext_id = h.get("external_id")
        text = (h.get("text") or "")[:260].replace("\n", " ").strip()
        id_hint = f" (canonical id: {ext_id})" if ext_id else ""
        lines.append(
            f"[{i}] title={title}\n    source={source}{id_hint}\n    url={url}\n"
            f"    text={text}"
        )
    return "\n\n".join(lines)


async def _exa_judge(
    model,
    raw_title: str,
    hits: list[dict[str, Any]],
    resource: Any | None = None,
) -> dict | None:
    """Run the Exa-specific LLM judge. Returns a finalize dict or None.

    The judge is allowed to pick any of the provided web hits. The chosen hit's
    URL is parsed back into external_source/external_id by the caller.
    """
    if not hits:
        return None
    evidence_text = _build_exa_evidence_text(hits)
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
        f"Web search evidence:\n{evidence_text}\n\n"
        f"Return the finalize JSON now."
    )
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = await model.ainvoke(
            [SystemMessage(content=_EXA_JUDGE_SYSTEM_PROMPT), HumanMessage(content=user_msg)]
        )
    except Exception:
        logger.warning("[metadata_agent][exa_fallback] judge LLM call failed for %r", raw_title[:80])
        return None
    content = getattr(resp, "content", "") or ""
    if isinstance(content, list):
        content = "".join(getattr(c, "text", str(c)) for c in content)
    # Reuse the same JSON extractor as the wikipedia judge path.
    from app.services.metadata_wiki_judge import _parse_finalize_json

    return _parse_finalize_json(content)


# ---------------------------------------------------------------------------
# Public fallback entry
# ---------------------------------------------------------------------------

async def exa_fallback_judge(
    model,
    raw_title: str,
    resource: Any | None = None,
    *,
    exa_searcher=None,
) -> tuple[dict, dict] | None:
    """Exa web-search fallback for a wikipedia-not-found title.

    Returns a (finalize_dict, search_info) tuple:
      - found=True  -> match found on the web; search_info.error is None.
      - found=False -> Exa searched and found no credible match; definitive.
      - search_info.error set -> Exa itself failed (network/rate/API key);
        the caller should treat this as transient and not cache.

    Returns None when Exa is disabled/unconfigured, letting the caller fall
    back to existing ReAct / not_found logic unchanged.
    """
    searcher = exa_searcher or _exa_web_search
    if exa_searcher is None and (
        not runtime_config.exa_api_key or not runtime_config.exa_enabled
    ):
        return None

    # Build a query: raw title + light source-language context. Exa neural
    # search handles the rest, including cross-script variants.
    lang_ctx = ""
    if resource is not None:
        raw = raw_title
        # crude language detection for context
        if any("一" <= ch <= "鿿" for ch in raw):
            lang_ctx = "动漫 改编"
        elif any("぀" <= ch <= "ゟ" or "゠" <= ch <= "ヿ" for ch in raw):
            lang_ctx = "アニメ ドラマ"
    query = f"{raw_title} {lang_ctx}".strip()

    try:
        hits = await searcher(query)
    except Exception as e:
        err = f"exa search failed: {type(e).__name__}: {e}"[:200]
        logger.warning("[metadata_agent][exa_fallback] %s for %r", err, raw_title[:80])
        return (
            {"found": False, "clean_title": raw_title, "content_type": "tv",
             "reason": err},
            {"method": "search_then_exa_fallback",
             "data_sources_used": ["exa"],
             "source_errors": {"exa": err},
             "error": err},
        )

    if not hits:
        return (
            {"found": False, "clean_title": raw_title, "content_type": "tv",
             "reason": "no credible match in Exa web search"},
            {"method": "search_then_exa_fallback",
             "data_sources_used": ["exa"],
             "source_errors": {},
             "error": None},
        )

    finalize_dict = await _exa_judge(model, raw_title, hits, resource=resource)
    if finalize_dict is None:
        # Unparseable judge JSON - treat like a transient infra failure so the
        # resource is retried (we cannot safely declare a definitive not_found).
        err = "exa judge returned unparseable JSON"
        return (
            {"found": False, "clean_title": raw_title, "content_type": "tv",
             "reason": err},
            {"method": "search_then_exa_fallback",
             "data_sources_used": ["exa"],
             "source_errors": {"exa": err},
             "error": err},
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
            me.get("wikipedia_url")
            or me.get("url")
            or _guess_url_from_description(me.get("description") or "")
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
        finalize_dict["matched_entity"] = me

    return finalize_dict, {
        "method": "search_then_exa_fallback",
        "data_sources_used": ["exa"],
        "source_errors": {},
        "error": None,
    }


def _guess_url_from_description(description: str) -> str:
    """Pull the first http URL out of a description fallback."""
    m = re.search(r"https?://[^\s\)<>\"]+", description or "")
    return m.group(0) if m else ""


__all__ = ["exa_fallback_judge", "_exa_web_search", "_source_and_id_from_url"]
