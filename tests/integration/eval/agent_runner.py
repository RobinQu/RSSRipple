"""Batch runner for the MetadataAgent.

Runs ``UnifiedMetadataAgent.process_title_only()`` against a list of
raw titles with concurrency control.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("rssripple.eval")


@dataclass
class AgentRunResult:
    """Result of running the MetadataAgent on a single title."""

    title_id: str
    title_raw: str
    source_feed: str
    resource_metadata: dict | None = None
    latency_ms: float = 0.0
    error: str | None = None


def _resource_metadata_to_dict(meta: Any) -> dict:
    """Convert a ResourceMetadata instance to a plain dict.

    Handles both ResourceMetadata objects and fallback dicts.
    Serializes all fields including ``matched_entity``.
    """
    if meta is None:
        return {
            "found": False,
            "clean_title": "",
            "content_type": "tv",
            "confidence": 0.0,
        }

    # Already a dict — return as-is
    if isinstance(meta, dict):
        return meta

    d: dict[str, Any] = {
        "clean_title": getattr(meta, "clean_title", ""),
        "content_type": getattr(meta, "content_type", "tv"),
        "found": getattr(meta, "found", False),
        "title_cn": getattr(meta, "title_cn", None),
        "title_en": getattr(meta, "title_en", None),
        "episode": getattr(meta, "episode", None),
        "season": getattr(meta, "season", None),
        "resolution": getattr(meta, "resolution", None),
        "source": getattr(meta, "source", None),
        "video_codec": getattr(meta, "video_codec", None),
        "audio_codec": getattr(meta, "audio_codec", None),
        "subtitle_type": getattr(meta, "subtitle_type", None),
        "subtitle_group": getattr(meta, "subtitle_group", None),
        "container": getattr(meta, "container", None),
        "confidence": getattr(meta, "confidence", 0.0),
        "reason": getattr(meta, "reason", None),
        "ambiguous": getattr(meta, "ambiguous", False),
        "ambiguous_candidates": getattr(meta, "ambiguous_candidates", []),
        "search_method": getattr(meta, "search_method", None),
        "data_sources_used": getattr(meta, "data_sources_used", []),
        "source_errors": getattr(meta, "source_errors", {}),
        "search_error": getattr(meta, "search_error", None),
    }

    # matched_entity may be a dict or None
    entity = getattr(meta, "matched_entity", None)
    d["matched_entity"] = entity if isinstance(entity, dict) else (dict(entity) if entity else None)

    return d


async def _process_single(
    title: dict[str, str],
    semaphore: asyncio.Semaphore,
) -> AgentRunResult:
    """Run the agent for a single title under a semaphore."""
    title_id = title["id"]
    raw_title = title["raw_title"]
    source_feed = title.get("source_feed", "unknown")
    data_source_type = title.get("data_source_type") or "exa"

    async with semaphore:
        start = time.monotonic()
        try:
            from app.services.metadata_agent import get_agent

            logger.info(
                "[eval][agent] start title_id=%s source=%s feed=%s raw_title=%r",
                title_id, data_source_type, source_feed, raw_title[:240],
            )
            agent = get_agent()
            result = await agent.process_title_only(raw_title, data_source_type)

            latency_ms = (time.monotonic() - start) * 1000
            meta_dict = _resource_metadata_to_dict(result)
            logger.info(
                "[eval][agent] done title_id=%s source=%s latency_ms=%.1f found=%s clean_title=%r method=%s sources=%s error=%s entity=%s",
                title_id,
                data_source_type,
                latency_ms,
                meta_dict.get("found"),
                (meta_dict.get("clean_title") or "")[:120],
                meta_dict.get("search_method"),
                meta_dict.get("data_sources_used"),
                meta_dict.get("search_error"),
                meta_dict.get("matched_entity"),
            )
            return AgentRunResult(
                title_id=title_id,
                title_raw=raw_title,
                source_feed=source_feed,
                resource_metadata=meta_dict,
                latency_ms=round(latency_ms, 1),
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            logger.exception(
                "[eval][agent] failed title_id=%s source=%s latency_ms=%.1f raw_title=%r",
                title_id, data_source_type, latency_ms, raw_title[:240],
            )
            return AgentRunResult(
                title_id=title_id,
                title_raw=raw_title,
                source_feed=source_feed,
                latency_ms=round(latency_ms, 1),
                error=str(exc),
            )


async def run_agent_on_titles(
    titles: list[dict[str, str]],
    max_concurrency: int = 3,
) -> list[dict[str, Any]]:
    """Run the MetadataAgent on a batch of titles concurrently.

    Parameters
    ----------
    titles:
        List of dicts with keys ``id``, ``raw_title``, ``source_feed``.
    max_concurrency:
        Maximum number of concurrent agent invocations (default 3).

    Returns
    -------
    list[dict]
        Each item is a dict with keys ``title_id``, ``title_raw``,
        ``source_feed``, ``resource_metadata``, ``latency_ms``, ``error``.
    """
    if not titles:
        logger.info("[eval][agent] run_agent_on_titles empty batch")
        return []

    logger.info(
        "[eval][agent] batch start total=%d max_concurrency=%d sources=%s",
        len(titles),
        max_concurrency,
        sorted({str(t.get("data_source_type") or "exa") for t in titles}),
    )
    semaphore = asyncio.Semaphore(max_concurrency)

    tasks = [_process_single(t, semaphore) for t in titles]
    results: list[AgentRunResult] = await asyncio.gather(*tasks)
    logger.info("[eval][agent] batch done total=%d errors=%d", len(results), sum(1 for r in results if r.error))

    return [
        {
            "title_id": r.title_id,
            "title_raw": r.title_raw,
            "source_feed": r.source_feed,
            "resource_metadata": r.resource_metadata,
            "latency_ms": r.latency_ms,
            "error": r.error,
        }
        for r in results
    ]
