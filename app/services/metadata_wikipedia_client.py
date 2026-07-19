"""Wikipedia HTTP client (wikipediaapi wrapper) + page/image fetch helpers.

Pure leaf module - no DB, no LLM, no agent state. Extracted verbatim from
metadata_agent.py (Phase 1): the Wikimedia-compliant client, disambiguation
detection, the thread-bridge ``_wiki_call`` that normalizes library exceptions
to a transient ``_WikipediaRequestError`` (so infra failures stay retryable),
and the search/page/image-fetch primitives consumed by the S3 judge path and
the audio resolver.
"""
from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wikipedia
#
# Backed by the maintained ``Wikipedia-API`` PyPI package (import
# ``wikipediaapi``), which replaces the unmaintained ``wikipedia`` package
# (v1.4.0). The old package was fragile in three ways that all converged on
# the same ``JSONDecodeError`` ("Expecting value: line 1 column 1 (char 0)"):
#   * its ``User-Agent`` defaulted to a generic string that Wikimedia's UA
#     policy throttles/blocks with an *empty* response body;
#   * it built ``http://<lang>.wikipedia.org/...`` URLs (Wikimedia redirects
#     to https, which some networks/proxies drop to an empty body);
#   * ``_wiki_request`` called ``r.json()`` with no ``raise_for_status`` and
#     no retry, so an empty body raised ``JSONDecodeError``.
# ``Wikipedia-API`` alleviates all three at the source:
#   * the constructor REQUIRES a descriptive ``user_agent`` (>=5 chars) and
#     builds a Wikimedia-compliant composite UA header;
#   * it hits ``https://{lang}.wikipedia.org/w/api.php`` directly (no http
#     redirect whose body can be dropped);
#   * it checks HTTP status (non-200 -> ``WikiHttpError``) instead of parsing
#     an empty body, and retries transient failures (5xx, 429, connection,
#     timeout, invalid JSON) internally up to ``max_retries``.
# We still wrap every call so any residual ``WikipediaException`` is mapped to
# a transient ``_WikipediaRequestError`` ("Wikipedia request failed: ..."),
# never a definitive "no match" - so the backfill retries instead of caching a
# permanent not_found. A genuine page-not-found (``page.exists() is False``)
# returns a non-transient "Page not found" error.
# ---------------------------------------------------------------------------

# Wikimedia-compliant UA - policy requires a descriptive agent with contact.
_WIKIPEDIA_USER_AGENT = (
    f"{settings.app_name}/0.1.0 (https://github.com/RobinQu/RSSRipple) "
    f"metadata-agent"
)


class _WikipediaRequestError(Exception):
    """A retryable infra failure from the wikipediaapi library (connection,
    timeout, rate limit, non-200, invalid JSON). Its message always starts
    with ``"Wikipedia request failed"`` so :func:`_classify_failure` treats
    the outcome as transient."""


@lru_cache(maxsize=8)
def _wikipedia_client(lang: str) -> Any:
    """Build (and cache) a ``wikipediaapi.Wikipedia`` client for one language.

    The client carries a Wikimedia-compliant User-Agent, uses HTTPS, and
    retries transient HTTP failures (5xx, 429, connection, timeout, invalid
    JSON) internally - so the empty-body ``JSONDecodeError`` that plagued the
    old ``wikipedia`` package cannot occur.
    """
    import wikipediaapi

    return wikipediaapi.Wikipedia(
        user_agent=_WIKIPEDIA_USER_AGENT,
        language=lang,
        extract_format=wikipediaapi.ExtractFormat.WIKI,
    )


def _is_disambiguation_category(category_names: list[str]) -> bool:
    """Heuristic disambiguation detection from a page's category names.

    ``Wikipedia-API`` does not raise ``DisambiguationError`` (unlike the old
    package); a disambiguation page is a normal page whose categories include
    a disambiguation category. We detect that so the agent can ask for a more
    specific title instead of trusting a generic page.
    """
    for name in category_names:
        lowered = name.lower()
        if "disambig" in lowered or "消歧义" in name or "曖昧" in name:
            return True
    return False


async def _wiki_call(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous ``wikipediaapi`` call in a worker thread, mapping any
    library exception to a transient ``_WikipediaRequestError``.

    Transient retrying (5xx, 429, connection, timeout, invalid JSON) is handled
    inside the library; this wrapper only normalizes the error contract so the
    agent's failure classification treats infra failures as retryable.
    """
    import wikipediaapi

    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except wikipediaapi.WikipediaException as e:
        raise _WikipediaRequestError(
            f"Wikipedia request failed: {type(e).__name__} ({e})"
        )
    except Exception as e:  # noqa: BLE001 - belt-and-suspenders for httpx errors
        msg = str(e).lower()
        if "timeout" in msg or "timed out" in msg or "connection" in msg:
            raise _WikipediaRequestError(
                f"Wikipedia request failed: network error ({type(e).__name__})"
            )
        raise _WikipediaRequestError(
            f"Wikipedia request failed: {type(e).__name__} ({e})"
        )


async def _execute_search_wikipedia(query: str, lang: str = "en") -> dict:
    """Search Wikipedia for matching pages."""
    try:
        import wikipediaapi  # noqa: F401 - presence check only
    except ImportError as e:
        return {"success": False, "data": [], "error": f"wikipediaapi not installed: {e}"}

    wiki_lang = lang if lang in ("en", "zh", "ja") else "en"
    wiki = _wikipedia_client(wiki_lang)

    try:
        results = await _wiki_call(wiki.search, query, limit=8)
    except _WikipediaRequestError as e:
        logger.warning(
            "[metadata_agent] search_wikipedia failed for query=%r lang=%s: %s",
            query, lang, e,
        )
        return {"success": False, "data": [], "error": str(e)}
    if not results or not getattr(results, "pages", None):
        return {"success": True, "data": []}

    pages = []
    for _title, page in list(results.pages.items())[:5]:
        # ``pageid`` is populated by ``search``; ``exists()`` is a cheap
        # ``pageid > 0`` check (no extra API call) for search-result stubs.
        if not page.exists():
            continue
        # ``summary`` is a lazy extract (one API call per page); a transient
        # failure on one page must not sink the whole result, so fall back to "".
        try:
            summary = await _wiki_call(lambda p=page: p.summary)
        except _WikipediaRequestError as e:
            logger.debug(
                "[metadata_agent] wikipedia summary(%r) failed lang=%s: %s",
                page.title, wiki_lang, e,
            )
            summary = ""
        pages.append(
            {
                "title": page.title,
                "page_id": page.pageid,
                "url": page.fullurl,
                "summary": (summary or "")[:500],
            }
        )
    return {"success": True, "data": pages}


async def _fetch_wikipedia_page_image(
    title: str, lang: str = "en", page_id: int | None = None,
    expected_title: str | None = None,
) -> str | None:
    """Fetch the lead/infobox image URL for a Wikipedia page.

    Two MediaWiki endpoints are tried in order, because neither alone covers
    every page:

    1. ``action=query&prop=pageimages`` (queried by ``pageids`` when available
       for reliable redirect resolution, else by ``titles``). Fast and
       batchable, but the PageImages extension has not assessed a lead image
       for some pages (e.g. zh ``二十世纪电气目录``).
    2. The REST ``/api/rest_v1/page/summary/{title}`` endpoint, whose
       ``originalimage``/``thumbnail`` fields surface the lead image for pages
       the pageimages prop misses.

    Returns the original (full-res) source when available, falling back to a
    500px thumbnail. Returns ``None`` on any failure or when the page has no
    image - callers must treat the absence of a poster as non-fatal.
    """
    from urllib.parse import quote

    import httpx

    from app.services.metadata_service import AUTO_LINK_THRESHOLD
    from app.services.text_normalizer import similarity_score

    wiki_lang = lang if lang in ("en", "zh", "ja") else "en"
    headers = {"Accept": "application/json", "User-Agent": _WIKIPEDIA_USER_AGENT}

    def _title_ok(page_title: str | None) -> bool:
        """True when the fetched page's title plausibly belongs to this work.
        Without this guard a stale/wrong ``page_id`` (or a REST title mismatch)
        silently returns an unrelated article's image."""
        if not expected_title or not page_title:
            return True
        return similarity_score(expected_title, page_title) >= AUTO_LINK_THRESHOLD

    # ── 1. pageimages prop ──
    pi_params = {
        "action": "query",
        "format": "json",
        "prop": "pageimages",
        "piprop": "original|thumbnail",
        "pithumbsize": 500,
        "redirects": 1,
    }
    if page_id:
        pi_params["pageids"] = page_id
    else:
        pi_params["titles"] = title
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://{wiki_lang}.wikipedia.org/w/api.php",
                params=pi_params,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        for page in ((data.get("query") or {}).get("pages") or {}).values():
            if not _title_ok(page.get("title")):
                # The stored page_id points at the wrong article - fall through
                # to the REST-by-title fallback instead of trusting it.
                continue
            original = page.get("original")
            if isinstance(original, dict) and original.get("source"):
                return original["source"]
            thumb = page.get("thumbnail")
            if isinstance(thumb, dict) and thumb.get("source"):
                return thumb["source"]
    except Exception as e:  # noqa: BLE001 - best-effort image fetch
        logger.debug(
            "[metadata_agent] wikipedia pageimages(%r/%s) failed lang=%s: %s",
            title, page_id, wiki_lang, e,
        )

    # ── 2. REST summary fallback ──
    # Queried by ``title`` (not pageid), so when the caller's title is not the
    # canonical article title it can resolve to a different page and surface an
    # unrelated image. ``_title_ok`` rejects those the same way.
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"https://{wiki_lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title)}",
                headers=headers,
            )
            if resp.status_code != 200:
                return None
            d = resp.json()
        if not _title_ok(d.get("title")):
            logger.debug(
                "[metadata_agent] wikipedia rest_summary(%r) page title %r "
                "did not match expected %r - skipping image",
                title, d.get("title"), expected_title,
            )
            return None
        original = d.get("originalimage")
        if isinstance(original, dict) and original.get("source"):
            return original["source"]
        thumb = d.get("thumbnail")
        if isinstance(thumb, dict) and thumb.get("source"):
            return thumb["source"]
    except Exception as e:  # noqa: BLE001 - best-effort image fetch
        logger.debug(
            "[metadata_agent] wikipedia rest_summary(%r) failed lang=%s: %s",
            title, wiki_lang, e,
        )
    return None


async def _execute_get_wikipedia_page(title: str, lang: str = "en") -> dict:
    """Get full Wikipedia page with infobox and categories."""
    try:
        import wikipediaapi  # noqa: F401 - presence check only
    except ImportError as e:
        return {"success": False, "data": {}, "error": f"wikipediaapi not installed: {e}"}

    wiki_lang = lang if lang in ("en", "zh", "ja") else "en"
    wiki = _wikipedia_client(wiki_lang)
    page = wiki.page(title)  # lazy stub - no network until an attr is resolved

    try:
        exists = await _wiki_call(page.exists)
    except _WikipediaRequestError as e:
        logger.warning(
            "[metadata_agent] get_wikipedia_page failed for title=%r lang=%s: %s",
            title, lang, e,
        )
        return {"success": False, "data": {}, "error": str(e)}
    if not exists:
        return {"success": False, "data": {}, "error": f"Page not found: {title}"}

    # ``categories`` is a lazy fetch; reuse it for both disambiguation
    # detection and the result so we only pay for one API call.
    try:
        categories = await _wiki_call(lambda p=page: list(p.categories.keys()))
    except _WikipediaRequestError as e:
        logger.debug(
            "[metadata_agent] wikipedia categories(%r) failed lang=%s: %s",
            title, wiki_lang, e,
        )
        categories = []

    if _is_disambiguation_category(categories or []):
        return {
            "success": True,
            "data": {
                "title": title,
                "disambiguation": True,
                "options": [],
            },
        }

    try:
        summary = await _wiki_call(lambda p=page: p.summary)
    except _WikipediaRequestError as e:
        logger.debug(
            "[metadata_agent] wikipedia summary(%r) failed lang=%s: %s",
            title, wiki_lang, e,
        )
        summary = ""

    poster_url = await _fetch_wikipedia_page_image(
        page.title, wiki_lang, page.pageid, expected_title=page.title
    )

    return {
        "success": True,
        "data": {
            "title": page.title,
            "page_id": page.pageid,
            "url": page.fullurl,
            "summary": (summary or "")[:800],
            "categories": (categories or [])[:20],
            "poster_url": poster_url,
        },
    }
