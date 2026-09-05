"""TMDB metadata search helpers.

Callers choose one source explicitly; this module performs no layered
fallback search. Results are uniform candidate dicts that drop into the
existing ``create_or_update_*_from_external()`` functions unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any

import httpx
from httpx import HTTPStatusError, TimeoutException

from app.services.anime_signals import is_anime_from_tmdb
from app.services.genre_registry import TMDB_ID_TO_NAME
from app.services.runtime_config import runtime_config

logger = logging.getLogger(__name__)


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
# Validation gate
# ---------------------------------------------------------------------------


def _validate_candidate(c: dict[str, Any]) -> bool:
    """Return True if the candidate has enough information to be useful."""
    has_title = bool(c.get("title_cn") or c.get("title_en") or c.get("original_title"))
    has_content_type = c.get("content_type") in ("tv", "movie")
    return has_title and has_content_type
