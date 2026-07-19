"""AudioWork resolver for the metadata agent.

Resolves audio-marked resources (ASMR / music / drama CD / radio) into
AudioWork entities: local title match -> Wikipedia/Exa search -> title-stub
fallback. Extracted from ``metadata_agent`` (Phase 3); the agent keeps a thin
delegating ``_resolve_audio_work`` method so ``process``'s ``self._resolve_audio_work``
call is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.metadata_resource_meta import ResourceMetadata
from app.services.metadata_source_io import _execute_search_exa_agent
from app.services.metadata_sources import resolve_metadata_source
from app.services.metadata_wiki_query import _CJK_RE, _candidate_queries
from app.services.metadata_wikipedia_client import (
    _execute_get_wikipedia_page,
    _execute_search_wikipedia,
)
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


async def _search_audio_wikipedia(
    raw_title: str, search_title: str
) -> dict | None:
    """Best-effort Wikipedia match for an audio work. Returns a matched
    entity dict (``external_id`` = ``wikipedia:<page_id>``) or None."""
    from app.services.text_normalizer import similarity_score

    queries = _candidate_queries(raw_title, None)
    if not queries:
        queries = [(search_title, "zh" if _CJK_RE.search(search_title) else "en")]
    results = await asyncio.gather(
        *(_execute_search_wikipedia(q, lang) for (q, lang) in queries),
        return_exceptions=True,
    )
    best: dict | None = None
    best_score = 0
    best_lang = "en"
    for (q, lang), res in zip(queries, results):
        if isinstance(res, Exception) or not isinstance(res, dict) or not res.get("success"):
            continue
        for cand in res.get("data", [])[:3]:
            title = cand.get("title") or ""
            score = similarity_score(search_title, title)
            if score > best_score:
                best_score = score
                best = cand
                best_lang = lang
    if best is None or best_score < 75:
        return None
    page_id = best.get("page_id")
    url = best.get("url")
    desc = best.get("summary") or ""
    # Fetch the full page for a richer description + canonical url.
    page = await _execute_get_wikipedia_page(best.get("title", ""), best_lang)
    if isinstance(page, dict) and page.get("data"):
        d = page["data"]
        desc = (d.get("summary") or desc)[:500]
        url = d.get("url") or url
        page_id = d.get("page_id") or page_id
    return {
        "title_cn": best.get("title"),
        "external_id": f"wikipedia:{page_id}" if page_id else None,
        "external_source": "wikipedia",
        "description": desc or None,
        "wikipedia_url": url,
    }


async def _search_audio_exa(search_title: str) -> dict | None:
    """Best-effort Exa match for an audio work. Returns a matched entity
    dict or None."""
    res = await _execute_search_exa_agent(search_title)
    if not isinstance(res, dict) or not res.get("success"):
        return None
    data = res.get("data") or []
    if not data:
        return None
    cand = data[0]
    return {
        "title_cn": cand.get("title_cn") or cand.get("title_en") or cand.get("title"),
        "title_en": cand.get("title_en"),
        "external_id": cand.get("external_id"),
        "external_source": cand.get("external_source") or "exa",
        "description": cand.get("description"),
        "poster_url": cand.get("poster_url"),
    }


async def _resolve_audio_work(
    resource: Any,
    channel: Any,
    db: AsyncSession,
    audio_type: str,
    force_refresh: bool,  # noqa: ARG002 - kept for signature parity
) -> ResourceMetadata | None:
    """Resolve an audio-marked resource into an AudioWork entity.

    Tries a local match first (no search), then a general-purpose source
    search (Wikipedia / Exa; TMDB falls back to Wikipedia). On no external
    match, creates a title-stub AudioWork so the resource is grouped and
    never retried. Links ``resource.audio_work_id`` and returns a
    found=True ``ResourceMetadata``.
    """
    from app.services.metadata_service import (
        AUTO_LINK_THRESHOLD,
        create_or_update_audio_work_from_external,
        extract_search_title,
        match_audio_work_by_title,
    )

    raw_title = getattr(resource, "title_raw", "") or ""
    search_title = (
        getattr(resource, "search_title", None)
        or extract_search_title(resource)
        or raw_title
    )[:200]
    if not search_title.strip():
        return None

    # 1. Local match - reuse an existing AudioWork for this title.
    existing, score = await match_audio_work_by_title(db, search_title)
    if existing is not None and score >= AUTO_LINK_THRESHOLD:
        resource.audio_work_id = existing.id
        resource.series_id = None
        resource.movie_id = None
        resource.search_title = search_title
        resource.metadata_matched_at = utcnow()
        logger.info(
            "[metadata_agent] audio local-match %r -> %s (score=%d, no search)",
            raw_title[:80], existing.id, score,
        )
        return ResourceMetadata(
            clean_title=search_title,
            found=True,
            content_type=audio_type,
            matched_entity={"external_id": existing.external_id},
        )

    # 2. External search via a general-purpose source. Audio works have no
    # TMDB coverage, so TMDB/local channels fall back to Wikipedia (free).
    source = resolve_metadata_source(getattr(channel, "metadata_source", None))
    if source not in ("wikipedia", "exa"):
        source = "wikipedia"

    matched: dict | None = None
    try:
        if source == "wikipedia":
            matched = await _search_audio_wikipedia(raw_title, search_title)
        else:
            matched = await _search_audio_exa(search_title)
    except Exception as e:
        logger.warning(
            "[metadata_agent] audio %s search failed for %r: %s",
            source, raw_title[:60], e,
        )

    if matched is None:
        # 3. Stub from the cleaned title.
        matched = {"title_cn": search_title, "external_source": "stub"}
    matched.setdefault("title_cn", search_title)
    matched.setdefault("content_type", audio_type)

    audio = await create_or_update_audio_work_from_external(db, matched)
    resource.audio_work_id = audio.id
    resource.series_id = None
    resource.movie_id = None
    resource.search_title = search_title
    resource.metadata_matched_at = utcnow()
    logger.info(
        "[metadata_agent] audio resolved %r -> %s (%s)",
        raw_title[:80], audio.id, matched.get("external_source"),
    )
    return ResourceMetadata(
        clean_title=search_title,
        found=True,
        content_type=audio_type,
        matched_entity=matched,
    )
