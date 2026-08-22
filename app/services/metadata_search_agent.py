"""Single-source metadata search helpers.

TMDB is exposed as an independent data source; Jina provides search + reader
primitives for the ReAct agent. Callers must choose one source explicitly;
this module no longer performs layered fallback search.

All sources produce a uniform ``MetadataCandidate`` dict that drops into the
existing ``create_or_update_*_from_external()`` functions unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any, TypedDict

import httpx
from httpx import HTTPStatusError, TimeoutException

from app.services.anime_signals import is_anime_from_tmdb
from app.services.genre_registry import TMDB_ID_TO_NAME
from app.services.runtime_config import runtime_config
from app.services.url_tools import keep_k_per_hostname

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TypedDict
# ---------------------------------------------------------------------------


class MetadataCandidate(TypedDict, total=False):
    content_type: str  # "tv" | "movie"
    title_cn: str | None
    title_en: str | None
    original_title: str | None
    description: str | None
    poster_url: str | None
    year: int | None
    rating: float | None
    genre: list[str]
    status: str | None
    external_id: str
    external_source: str  # "tmdb" | "jina" | "llm_search"
    number_of_episodes: int | None
    number_of_seasons: int | None
    start_date: str | None
    end_date: str | None
    release_date: str | None
    runtime: int | None
    is_anime: bool | None  # deterministic TMDB verdict (see anime_signals)


# ---------------------------------------------------------------------------
# Session-level in-memory cache (per-process, single RSS fetch context)
# Bounded to 500 entries with LRU-like eviction. Keys older than 1 hour expire.
# ---------------------------------------------------------------------------

_CACHE_MAXSIZE = 500
_CACHE_TTL = 3600  # 1 hour in seconds

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _cache_key(source: str, title: str) -> str:
    return f"{source}:{title.lower().strip()}"


def _cache_get(source: str, title: str) -> list[dict[str, Any]] | None:
    key = _cache_key(source, title)
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, results = entry
    import time as _time
    if _time.monotonic() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return results


def _cache_set(source: str, title: str, results: list[dict[str, Any]]) -> None:
    import time as _time
    key = _cache_key(source, title)
    # Evict oldest entry if at capacity
    if len(_cache) >= _CACHE_MAXSIZE and key not in _cache:
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest_key]
    _cache[key] = (_time.monotonic(), results)


# ---------------------------------------------------------------------------
# TMDB source
# ---------------------------------------------------------------------------

TMDB_BASE = "https://api.themoviedb.org/3"
_JSON_MIME = "application/json"
_SEARCH_RESULT_LIMIT = 5

# Map TMDB status strings to RSSRipple-friendly values
_TMDB_TV_STATUS_MAP: dict[str, str] = {
    "Returning Series": "Returning Series",
    "Ended": "Ended",
    "Canceled": "Canceled",
    "Pilot": "Pilot",
    "In Production": "In Production",
    "Planned": "Planned",
}
_TMDB_MOVIE_STATUS_MAP: dict[str, str] = {
    "Released": "Released",
    "Post Production": "Post Production",
    "In Production": "In Production",
    "Planned": "Planned",
    "Rumored": "Rumored",
    "Canceled": "Canceled",
}


def _tmdb_poster_url(poster_path: str | None, image_base: str = "") -> str | None:
    if not poster_path:
        return None
    base = image_base or "https://image.tmdb.org/t/p/"
    return f"{base}w500{poster_path}"


@lru_cache(maxsize=1)
def _tmdb_image_base(api_key: str) -> str:
    """Fetch TMDB image base URL (cached for process lifetime)."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{TMDB_BASE}/configuration",
                params={"api_key": api_key},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("images", {}).get("secure_base_url", "https://image.tmdb.org/t/p/")
    except Exception:
        return "https://image.tmdb.org/t/p/"


# Static genre ID → name mapping now lives in the authoritative registry
# (``app.services.genre_registry.TMDB_ID_TO_NAME``); dynamic fetches are
# intersected with it so every emitted genre lands on the closed TMDB set.
_TMDB_GENRE_MAP: dict[int, str] | None = None


def _tmdb_genre_map(api_key: str) -> dict[int, str]:
    """Fetch TMDB genre name map (TV + Movie combined, cached for process lifetime)."""
    global _TMDB_GENRE_MAP
    if _TMDB_GENRE_MAP is not None:
        return _TMDB_GENRE_MAP
    result: dict[int, str] = {}
    try:
        with httpx.Client(timeout=10) as client:
            for kind in ("tv", "movie"):
                resp = client.get(
                    f"{TMDB_BASE}/genre/{kind}/list",
                    params={"api_key": api_key, "language": "en"},
                )
                resp.raise_for_status()
                for g in resp.json().get("genres", []):
                    gid = g["id"]
                    if gid in TMDB_ID_TO_NAME:
                        result[gid] = TMDB_ID_TO_NAME[gid]
                    else:
                        logger.debug(
                            "_tmdb_genre_map: dropping TMDB genre id outside registry: %r", g
                        )
        if not result:
            result = dict(TMDB_ID_TO_NAME)
    except Exception:
        # Static fallback: the authoritative registry's closed set.
        result = dict(TMDB_ID_TO_NAME)
    _TMDB_GENRE_MAP = result
    return result


def _resolve_genre_ids(genre_ids: list[int], api_key: str) -> list[str]:
    """Convert TMDB genre IDs to human-readable names."""
    if not genre_ids:
        return []
    gmap = _tmdb_genre_map(api_key)
    result: list[str] = []
    for gid in genre_ids:
        try:
            if gid in gmap:
                result.append(gmap[gid])
        except TypeError:
            logger = logging.getLogger("rssripple.eval")
            logger.warning(
                "[metadata_agent] _resolve_genre_ids: unhashable genre element type=%s value=%r",
                type(gid).__name__, gid,
            )
    return result


async def _search_tmdb(title: str) -> list[dict[str, Any]]:
    """Search TMDB for matching TV series and movies.

    Runs zh-CN + en-US searches in parallel and merges results by TMDB ID.
    """
    api_key = runtime_config.tmdb_api_key
    if not api_key:
        return []

    cached = _cache_get("tmdb", title)
    if cached is not None:
        return cached

    async def _search_lang(lang: str) -> list[dict]:
        """Run a single-language search_multi call."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{TMDB_BASE}/search/multi",
                    params={
                        "api_key": api_key,
                        "query": title,
                        "language": lang,
                        "include_adult": "false",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("results", [])
        except (HTTPStatusError, TimeoutException) as e:
            logger.warning(
                "[metadata_agent] TMDB search failed for lang=%s title=%r: %s",
                lang, title[:60], e,
            )
            return []
        except Exception as e:
            logger.warning(
                "[metadata_agent] TMDB search unexpected error lang=%s title=%r: %s",
                lang, title[:60], e,
            )
            return []

    zh_task = asyncio.create_task(_search_lang("zh-CN"))
    en_task = asyncio.create_task(_search_lang("en-US"))
    zh_results, en_results = await asyncio.gather(zh_task, en_task, return_exceptions=True)

    if isinstance(zh_results, BaseException):
        zh_results = []
    if isinstance(en_results, BaseException):
        en_results = []

    # Merge by TMDB ID: prefer zh-CN for title_cn, en-US for title_en
    merged: dict[int, dict] = {}

    def _ingest(items: list[dict], lang: str) -> None:
        for item in items:
            media_type = item.get("media_type", "")
            if media_type not in ("tv", "movie"):
                continue
            tmdb_id = item.get("id")
            if not tmdb_id:
                continue
            if tmdb_id not in merged:
                merged[tmdb_id] = {
                    "tmdb_id": tmdb_id,
                    "media_type": media_type,
                    "title_cn": None,
                    "title_en": None,
                    "original_title": None,
                    "overview": item.get("overview"),
                    "poster_path": item.get("poster_path"),
                    "vote_average": item.get("vote_average"),
                    "genre_ids": item.get("genre_ids", []),
                    "original_language": item.get("original_language"),
                    "origin_country": item.get("origin_country"),
                }
            entry = merged[tmdb_id]
            entry["media_type"] = entry["media_type"] or media_type
            entry["overview"] = entry["overview"] or item.get("overview")
            entry["poster_path"] = entry["poster_path"] or item.get("poster_path")
            entry["vote_average"] = entry["vote_average"] or item.get("vote_average")
            if not entry.get("genre_ids"):
                entry["genre_ids"] = item.get("genre_ids", [])
            if not entry.get("original_language"):
                entry["original_language"] = item.get("original_language")
            if not entry.get("origin_country"):
                entry["origin_country"] = item.get("origin_country")

            # Language-specific titles
            if lang == "zh-CN":
                name = item.get("name") or item.get("title")  # TV uses "name", movie uses "title"
                if name and not entry["title_cn"]:
                    entry["title_cn"] = name
                # Also capture original_title/name for zh-CN (might have native Chinese)
                orig = item.get("original_name") or item.get("original_title")
                if orig and not entry["original_title"]:
                    entry["original_title"] = orig
            else:  # en-US
                name = item.get("name") or item.get("title")
                if name and not entry["title_en"]:
                    entry["title_en"] = name
                orig = item.get("original_name") or item.get("original_title")
                if orig and not entry["original_title"]:
                    entry["original_title"] = orig

            # Dates
            if media_type == "tv":
                entry.setdefault("first_air_date", item.get("first_air_date"))
            else:
                entry.setdefault("release_date", item.get("release_date"))

    _ingest(zh_results, "zh-CN")
    _ingest(en_results, "en-US")

    if not merged:
        _cache_set("tmdb", title, [])
        return []

    # Both helpers use a sync httpx.Client internally — run them in a worker
    # thread so the (one-time, then cached) HTTP fetches never block the event
    # loop. After this warm-up, _resolve_genre_ids below is a pure dict lookup.
    image_base = await asyncio.to_thread(_tmdb_image_base, api_key)
    await asyncio.to_thread(_tmdb_genre_map, api_key)

    candidates: list[dict[str, Any]] = []
    for tmdb_id, m in sorted(merged.items(), key=lambda x: x[1].get("vote_average") or 0, reverse=True):
        ct = m["media_type"]  # "tv" or "movie"
        year_str = m.get("first_air_date") or m.get("release_date")  # type: ignore[union-attr]
        year = int(year_str[:4]) if year_str and len(year_str) >= 4 else None
        status_raw = None
        if ct == "tv":
            status_raw = _TMDB_TV_STATUS_MAP.get(m.get("status", ""))
        else:
            status_raw = _TMDB_MOVIE_STATUS_MAP.get(m.get("status", ""))

        candidate: dict[str, Any] = {
            "content_type": ct,
            "title_cn": m["title_cn"],
            "title_en": m["title_en"],
            "original_title": m["original_title"],
            "description": m.get("overview"),
            "poster_url": _tmdb_poster_url(m.get("poster_path"), image_base),
            "year": year,
            "rating": m.get("vote_average"),
            "genre": _resolve_genre_ids(m.get("genre_ids", []), api_key),
            "status": status_raw,
            "external_id": f"tmdb:{tmdb_id}",
            "external_source": "tmdb",
            "number_of_episodes": None,  # omitted (requires detail call)
            "number_of_seasons": None,
            "start_date": m.get("first_air_date"),
            "end_date": None,  # omitted
            "release_date": m.get("release_date"),
            "runtime": None,
            # Deterministic anime verdict from TMDB genre+language/country.
            "is_anime": is_anime_from_tmdb(
                m.get("genre_ids", []),
                m.get("original_language"),
                m.get("origin_country"),
            ),
        }
        if _validate_candidate(candidate):
            candidates.append(candidate)

    _cache_set("tmdb", title, candidates)
    return candidates


# ---------------------------------------------------------------------------
# Jina Search + Reader source
# ---------------------------------------------------------------------------

# Jina returns a flat envelope: {"code": 200, "status": 200, "data": [...]}.
# Search ``data`` is a list of SERP hits; Reader ``data`` is a single page dict.
_JINA_SEARCH_URL = "https://s.jina.ai/"
_JINA_READER_URL = "https://r.jina.ai/"
_JINA_SEARCH_TIMEOUT = 20.0
_JINA_READER_TIMEOUT = 60.0


def _jina_headers(extras: dict[str, str] | None = None) -> dict[str, str]:
    """Common Jina request headers. Caller passes the auth token explicitly."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Engine": "auto",  # let Jina pick browser/direct per URL
        "X-Retain-Images": "none",
        "X-Retain-Links": "text",
        "X-Md-Link-Style": "discarded",
    }
    if extras:
        headers.update(extras)
    return headers


async def _search_jina(query: str, num: int = 3) -> list[dict[str, Any]]:
    """Search the web via Jina Search API (``s.jina.ai``).

    Returns SERP hits as ``[{title, url, description, content}]`` where
    ``content`` is the markdown of each top page. Capped at 2 per hostname so
    a single site (Fandom, IMDB, …) can't dominate a small top-N. Cached by
    query like the TMDB/Exa sources.
    """
    if not runtime_config.jina_api_key:
        logger.info("[metadata_agent][jina] skipped query=%r: JINA_API_KEY not configured", query[:120])
        return []

    cached = _cache_get("jina", query)
    if cached is not None:
        logger.info("[metadata_agent][jina] cache hit query=%r hits=%d", query[:120], len(cached))
        return cached

    headers = _jina_headers({
        "Authorization": f"Bearer {runtime_config.jina_api_key}",
        "X-Preset": "agent",
        "X-Timeout": "20",
    })
    payload = {"q": query, "num": num, "gl": "us", "hl": "en"}

    try:
        async with httpx.AsyncClient(timeout=_JINA_SEARCH_TIMEOUT) as client:
            resp = await client.post(_JINA_SEARCH_URL, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except (HTTPStatusError, TimeoutException) as e:
        logger.warning(
            "[metadata_agent][jina] search failed query=%r: %s", query[:120], e,
        )
        _cache_set("jina", query, [])
        return []
    except Exception as e:
        logger.warning(
            "[metadata_agent][jina] search unexpected error query=%r: %s",
            query[:120], e, exc_info=True,
        )
        _cache_set("jina", query, [])
        return []

    raw_items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(raw_items, list):
        logger.info(
            "[metadata_agent][jina] no data[] in response query=%r body=%s",
            query[:120], _compact_obj(body, max_len=600),
        )
        _cache_set("jina", query, [])
        return []

    results: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("link")
        if not url:
            continue
        results.append({
            "title": item.get("title"),
            "url": url,
            "description": item.get("description"),
            "content": item.get("content"),
        })

    results = keep_k_per_hostname(results, k=2)
    logger.info(
        "[metadata_agent][jina] returning query=%r hits=%d (raw=%d)",
        query[:120], len(results), len(raw_items),
    )
    _cache_set("jina", query, results)
    return results


async def _read_jina_url(url: str, *, with_links: bool = False) -> dict[str, Any]:
    """Fetch a single URL's full content via Jina Reader API (``r.jina.ai``).

    Returns ``{title, url, description, content, links}``. Not cached — URLs
    are ephemeral and Jina Reader itself caches ~5 min on its side.
    """
    if not runtime_config.jina_api_key:
        logger.info("[metadata_agent][jina] read skipped url=%r: JINA_API_KEY not configured", url[:120])
        return {}

    extras: dict[str, str] = {
        "Authorization": f"Bearer {runtime_config.jina_api_key}",
        "X-Timeout": "60",
    }
    if with_links:
        extras["X-With-Links-Summary"] = "true"
    headers = _jina_headers(extras)
    payload = {"url": url}

    try:
        async with httpx.AsyncClient(timeout=_JINA_READER_TIMEOUT) as client:
            resp = await client.post(_JINA_READER_URL, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except (HTTPStatusError, TimeoutException) as e:
        logger.warning("[metadata_agent][jina] read failed url=%r: %s", url[:120], e)
        return {}
    except Exception as e:
        logger.warning(
            "[metadata_agent][jina] read unexpected error url=%r: %s",
            url[:120], e, exc_info=True,
        )
        return {}

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        logger.info(
            "[metadata_agent][jina] no data object in read response url=%r body=%s",
            url[:120], _compact_obj(body, max_len=600),
        )
        return {}

    return {
        "title": data.get("title"),
        "url": data.get("url") or url,
        "description": data.get("description"),
        "content": data.get("content"),
        "links": data.get("links") if with_links else None,
    }


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------


def _to_plain_obj(value: Any) -> Any:
    """Convert Pydantic/SDK objects into plain JSON-like values for logs/parsing."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_plain_obj(v) for v in value]
    if isinstance(value, tuple):
        return [_to_plain_obj(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_plain_obj(v) for k, v in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(by_alias=True, exclude_none=True)
        except TypeError:
            return model_dump()
    if hasattr(value, "__dict__"):
        return {
            k: _to_plain_obj(v)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    return repr(value)


def _compact_obj(value: Any, max_len: int = 800) -> str:
    """Render a compact, bounded JSON-ish string for verbose eval logs."""
    import json

    try:
        text = json.dumps(_to_plain_obj(value), ensure_ascii=False, default=str)
    except TypeError:
        text = repr(value)
    if len(text) > max_len:
        return text[:max_len] + "...<truncated>"
    return text


def _validate_candidate(c: dict[str, Any]) -> bool:
    """Return True if the candidate has enough information to be useful."""
    has_title = bool(c.get("title_cn") or c.get("title_en") or c.get("original_title"))
    has_content_type = c.get("content_type") in ("tv", "movie")
    return has_title and has_content_type


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_year(value: object) -> int | None:
    if not value:
        return None
    s = str(value).strip()
    if len(s) >= 4 and s[:4].isdigit():
        return int(s[:4])
    return None


def _fmt_date(value: object) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    if len(s) == 4 and s.isdigit():
        return f"{s}-01-01"
    return None


async def _validate_poster_url(url: str | None, max_retries: int = 3) -> str | None:
    """Validate a poster URL is a real, accessible image. Returns URL or None."""
    if not url:
        return None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.head(url, follow_redirects=True)
                if resp.status_code == 200:
                    ct = resp.headers.get("content-type", "")
                    if ct.startswith("image/"):
                        return url
                elif resp.status_code in (403, 405):
                    resp2 = await client.get(url, headers={"Range": "bytes=0-0"}, follow_redirects=True)
                    if resp2.status_code in (200, 206):
                        ct = resp2.headers.get("content-type", "")
                        if ct.startswith("image/"):
                            return url
        except Exception:
            if attempt == max_retries - 1:
                return None
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def search_metadata(
    title: str,
    data_source_type: str = "tmdb",
) -> list[dict[str, Any]]:
    """Search one selected metadata source.

    Returns a list of candidate dicts (same shape as legacy ``search_metadata_via_llm``)
    so callers in ``metadata_service`` work unchanged.
    """
    if not title or not title.strip():
        return []

    source = (data_source_type or "tmdb").strip().lower()
    if source == "combined":
        source = "tmdb"

    if source == "tmdb":
        try:
            merged = await _search_tmdb(title)
        except Exception as e:
            logger.warning("[metadata_agent] TMDB search exception: %s", e)
            return []

        def _sort_key(c: dict) -> float:
            r = c.get("rating")
            return float(r) if r is not None else 0.0

        merged.sort(key=_sort_key, reverse=True)
        return merged

    logger.warning("[metadata_agent] unsupported metadata_search_agent source=%s", source)
    return []
