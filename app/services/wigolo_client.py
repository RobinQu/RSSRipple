"""Minimal REST client for a self-hosted wigolo search daemon.

Pure leaf module - no DB, no LangGraph. Calls ``POST {base}/v1/search`` (one
REST route per wigolo tool; see the wigolo docs for the contract). Auth is a
bearer token when the daemon binds non-loopback; loopback daemons are open.

Raises :class:`WigoloSearchError` on transport/HTTP/tool errors so callers can
classify failures as transient. Degraded-but-successful responses stay 2xx
with in-body ``engine_warnings`` — surfaced via the logger, not an exception.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.runtime_config import runtime_config

logger = logging.getLogger(__name__)

# wigolo's server-side deadline for search is 60s; leave client headroom.
_TIMEOUT_SECONDS = 90.0


class WigoloSearchError(RuntimeError):
    """Raised when the wigolo search call fails (transport, HTTP, or tool)."""


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = runtime_config.wigolo_api_token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def web_search(
    query: str,
    num_results: int = 5,
    include_domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run one wigolo search; returns hits with title/url/text.

    ``include_domains`` pushes the fallback whitelist's domain allowlist down
    into the engines (subdomain matches count). Over-fetches slightly: the
    caller post-filters to identity-site hits, so headroom lifts recall.
    """
    base = (runtime_config.wigolo_base_url or "").rstrip("/")
    if not base:
        raise WigoloSearchError("wigolo_base_url is not configured")
    body: dict[str, Any] = {
        "query": query,
        # wigolo clamps max_results at 20.
        "max_results": max(8, min(int(num_results), 20)),
        "search_depth": "balanced",
    }
    if include_domains:
        body["include_domains"] = list(include_domains)

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.post(f"{base}/v1/search", json=body, headers=_headers())
        except httpx.HTTPError as e:
            raise WigoloSearchError(f"wigolo transport error: {type(e).__name__}: {e}") from e
    if resp.status_code >= 400:
        detail = resp.text[:200]
        raise WigoloSearchError(f"wigolo HTTP {resp.status_code}: {detail}")
    data = resp.json()

    warnings = data.get("engine_warnings") or []
    if warnings:
        logger.info(
            "[wigolo] degraded engines %s for %r",
            [w.get("engine") for w in warnings],
            query[:120],
        )
    error = data.get("error")
    if error:
        raise WigoloSearchError(f"wigolo tool error: {str(error)[:200]}")

    hits: list[dict[str, Any]] = []
    for r in data.get("results", []):
        hits.append(
            {
                "title": r.get("title", "") or "",
                "url": r.get("url", "") or "",
                "text": r.get("snippet", "") or "",
            }
        )
    logger.info(
        "[wigolo] search done query=%r hits=%d engines=%s total_ms=%s",
        query[:120],
        len(hits),
        data.get("engines_used"),
        data.get("total_time_ms"),
    )
    return hits


__all__ = ["WigoloSearchError", "web_search"]
