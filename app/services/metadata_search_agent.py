"""Multi-source metadata search agent.

Replaces the single-LLM web-search call in metadata_service with a layered
search strategy:

1. TMDB API (httpx) — primary, structured, free
2. Exa AI Agent API — web fallback with structured extraction

All sources produce a uniform ``MetadataCandidate`` dict that drops into the
existing ``create_or_update_*_from_external()`` functions unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, UTC
from functools import lru_cache
from typing import Any, TypedDict

import httpx
from httpx import HTTPStatusError, TimeoutException

from app.config import settings

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
    external_source: str  # "tmdb" | "exa" | "llm_search"
    number_of_episodes: int | None
    number_of_seasons: int | None
    start_date: str | None
    end_date: str | None
    release_date: str | None
    runtime: int | None


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


# Static genre ID → name mapping (most common TMDB genre IDs)
# Falls back to a dynamic fetch for uncommon IDs.
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
                    result[g["id"]] = g["name"]
    except Exception:
        # Static fallback for most common genres
        result = {
            28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy",
            80: "Crime", 99: "Documentary", 18: "Drama", 10751: "Family",
            14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
            9648: "Mystery", 10749: "Romance", 878: "Science Fiction",
            10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
            10759: "Action & Adventure", 10762: "Kids", 10763: "News",
            10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Soap",
            10767: "Talk", 10768: "War & Politics",
        }
    _TMDB_GENRE_MAP = result
    return result


def _resolve_genre_ids(genre_ids: list[int], api_key: str) -> list[str]:
    """Convert TMDB genre IDs to human-readable names."""
    if not genre_ids:
        return []
    gmap = _tmdb_genre_map(api_key)
    return [gmap[gid] for gid in genre_ids if gid in gmap]


async def _search_tmdb(title: str) -> list[dict[str, Any]]:
    """Search TMDB for matching TV series and movies.

    Runs zh-CN + en-US searches in parallel and merges results by TMDB ID.
    """
    api_key = settings.tmdb_api_key
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
                }
            entry = merged[tmdb_id]
            entry["media_type"] = entry["media_type"] or media_type
            entry["overview"] = entry["overview"] or item.get("overview")
            entry["poster_path"] = entry["poster_path"] or item.get("poster_path")
            entry["vote_average"] = entry["vote_average"] or item.get("vote_average")
            if not entry.get("genre_ids"):
                entry["genre_ids"] = item.get("genre_ids", [])

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

    image_base = _tmdb_image_base(api_key)

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
        }
        if _validate_candidate(candidate):
            candidates.append(candidate)

    _cache_set("tmdb", title, candidates)
    return candidates


# ---------------------------------------------------------------------------
# Exa AI Agent source
# ---------------------------------------------------------------------------

_EXA_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content_type": {
            "type": "string",
            "enum": ["tv", "movie"],
            "description": "Content type: 'tv' for TV series/anime, 'movie' for films",
        },
        "title_cn": {
            "type": ["string", "null"],
            "description": "Chinese title of the work",
        },
        "title_en": {
            "type": ["string", "null"],
            "description": "English title of the work",
        },
        "original_title": {
            "type": ["string", "null"],
            "description": "Original language title of the work",
        },
        "description": {
            "type": ["string", "null"],
            "description": "Brief plot summary or description",
        },
        "poster_url": {
            "type": ["string", "null"],
            "format": "uri",
            "description": "Direct URL to a poster image (.png/.jpg), not a webpage",
        },
        "year": {
            "type": ["integer", "null"],
            "description": "Release year (e.g. 2024)",
        },
        "rating": {
            "type": ["number", "null"],
            "description": "Rating score (0-10 scale)",
        },
        "genre": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genre tags",
        },
        "status": {
            "type": ["string", "null"],
            "description": "For TV: 'Returning Series'/'Ended'/'Canceled' etc. For movie: 'Released'/'Post Production' etc.",
        },
        "external_id": {
            "type": ["string", "null"],
            "description": "External identifier (e.g. TMDB ID, IMDB ID) if available",
        },
        "number_of_episodes": {
            "type": ["integer", "null"],
            "description": "Total number of episodes (TV only)",
        },
        "number_of_seasons": {
            "type": ["integer", "null"],
            "description": "Total number of seasons (TV only)",
        },
        "start_date": {
            "type": ["string", "null"],
            "format": "date",
            "description": "First air date (TV only, YYYY-MM-DD format)",
        },
        "end_date": {
            "type": ["string", "null"],
            "format": "date",
            "description": "Last air date (TV only, YYYY-MM-DD format)",
        },
        "release_date": {
            "type": ["string", "null"],
            "format": "date",
            "description": "Release date (movie, YYYY-MM-DD format)",
        },
        "runtime": {
            "type": ["integer", "null"],
            "description": "Runtime in minutes (movie)",
        },
    },
    "required": ["content_type"],
}


async def _search_exa(title: str) -> list[dict[str, Any]]:
    """Search for metadata via Exa AI Agent API."""
    if not settings.exa_api_key:
        return []

    cached = _cache_get("exa", title)
    if cached is not None:
        return cached

    try:
        from exa_py import AsyncExa

        exa = AsyncExa(api_key=settings.exa_api_key)
        run = await exa.agent.runs.create(
            query=(
                f'Search for metadata about "{title}". '
                f"Determine if this is a TV series/anime or a movie. "
                f"Find the Chinese and English titles, original title, a brief description, "
                f"a direct poster image URL (.png or .jpg, not a webpage), release year, "
                f"rating (0-10 scale), genre tags, status, and type-specific information "
                f"(number of episodes/seasons for TV, release date and runtime for movies)."
            ),
            output_schema=_EXA_OUTPUT_SCHEMA,
            effort=settings.exa_effort_level,
        )
        polled = await exa.agent.runs.poll_until_finished(
            run.id, poll_interval=4000, timeout_ms=300_000,
        )

        if polled.status == "completed" and polled.output and polled.output.structured:
            candidate_data = dict(polled.output.structured)
            candidate_data.setdefault("external_source", "exa")
            if not candidate_data.get("external_id"):
                candidate_data["external_id"] = "exa:" + hashlib.md5(
                    title.lower().encode()
                ).hexdigest()[:12]

            # Post-validate poster URL
            poster = candidate_data.get("poster_url")
            if poster:
                candidate_data["poster_url"] = await _validate_poster_url(poster)

            if _validate_candidate(candidate_data):
                _cache_set("exa", title, [candidate_data])
                return [candidate_data]

        # Non-completed or no structured output
        _cache_set("exa", title, [])
        return []

    except Exception as e:
        logger.warning("[metadata_agent] Exa search failed for %r: %s", title[:60], e)
        _cache_set("exa", title, [])
        return []


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------


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


async def search_metadata(title: str) -> list[dict[str, Any]]:
    """Multi-source metadata search.

    Returns a list of candidate dicts (same shape as legacy ``search_metadata_via_llm``)
    so callers in ``metadata_service`` work unchanged.

    Strategy:
    1. TMDB (always if API key set) — structured, high quality
    2. Exa AI Agent (if TMDB misses) — web search fallback with structured extraction
    """
    if not title or not title.strip():
        return []

    # Phase 1: TMDB
    try:
        merged = await _search_tmdb(title)
    except Exception as e:
        logger.warning("[metadata_agent] TMDB search exception: %s", e)
        merged = []

    # Sort by rating descending (if available)
    def _sort_key(c: dict) -> float:
        r = c.get("rating")
        return float(r) if r is not None else 0.0

    merged.sort(key=_sort_key, reverse=True)

    # Phase 2: Exa fallback if no structured results
    if not merged and settings.exa_api_key:
        exa_results = await _search_exa(title)
        merged.extend(exa_results)

    return merged
