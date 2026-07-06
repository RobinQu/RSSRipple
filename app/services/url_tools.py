"""URL normalization and diversity helpers for metadata search results.

Ported from ``jina-ai/node-DeepResearch`` (Apache-2.0) ``src/utils/url-tools.ts``.
Used by the Jina search source to dedupe and cap results; pure functions, no I/O.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking / analytics params that should not participate in URL identity.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_referrer",
    "gclid", "fbclid", "msclkid", "mc_eid", "_hsenc", "_hsmi",
    "ref", "ref_src", "ref_url", "referer", "source",
    "igshid", "si", "feature",
})


def normalize_url(url: str) -> str | None:
    """Return a canonical form of ``url``, or ``None`` when not a real URL.

    Strips tracking params, lowercases the host, unifies the scheme to
    ``https``, and drops a trailing slash on the path. Two URLs that differ
    only in UTM tags or ``http`` vs ``https`` collapse to the same string.
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.netloc:
        return None
    scheme = "https"
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    # Preserve non-tracking query params in stable order.
    cleaned_q = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(cleaned_q)
    return urlunsplit((scheme, netloc, path, query, ""))


def _hostname(url: str) -> str:
    return urlsplit(url).netloc.lower()


def keep_k_per_hostname(items: list[dict], k: int = 2) -> list[dict]:
    """Cap ``items`` at ``k`` entries per hostname, preserving first-seen order.

    ``items`` are dicts with a ``url`` (or ``link``) field. Prevents a single
    host (Fandom, IMDB, …) from monopolizing a small top-N result list.
    """
    if not items:
        return []
    out: list[dict] = []
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get("url") or item.get("link") or ""
        host = _hostname(str(raw)) if raw else ""
        if host and counts.get(host, 0) >= k:
            continue
        if host:
            counts[host] = counts.get(host, 0) + 1
        out.append(item)
    return out
