"""TMDB / Exa / Jina source I/O primitives.

Pure leaf module - no DB, no LLM. Extracted verbatim from metadata_agent.py
(Phase 1): the ``_execute_*`` HTTP wrappers that the LangGraph @tool layer
and the audio resolver call. TMDB genre/season resolution and Exa/Jina search
delegate to metadata_search_agent; TMDB details hit the TMDB API directly
using the configured api key.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.services.anime_signals import is_anime_from_tmdb
from app.services.runtime_config import runtime_config

logger = logging.getLogger(__name__)

# P4: caps for the per-season TMDB episode fetch. 30 seasons covers every
# realistic work; beyond that the per-season fan-out is not worth the calls.
TMDB_EPISODE_FETCH_MAX_SEASONS = 30
TMDB_EPISODE_FETCH_CONCURRENCY = 4


async def _execute_search_tmdb(query: str) -> dict:
    """Search TMDB — delegates to the existing metadata_search_agent module."""
    from app.services.metadata_search_agent import _search_tmdb

    try:
        results = await _search_tmdb(query)
        return {"success": True, "data": results}
    except Exception as e:
        logger.warning(
            "[metadata_agent] search_tmdb failed for query=%s: %s",
            query, e, exc_info=True,
        )
        return {"success": False, "data": [], "error": str(e)}


async def _execute_get_tmdb_details(tmdb_id: str, media_type: str) -> dict:
    """Fetch full TMDB details including season/episode structure."""
    from app.services.metadata_search_agent import _resolve_genre_ids, _tmdb_image_base

    api_key = runtime_config.tmdb_api_key
    if not api_key:
        return {"success": False, "data": {}, "error": "TMDB API key not configured"}

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}",
                params={"api_key": api_key, "language": "zh-CN"},
            )
            resp.raise_for_status()
            data = resp.json()

        image_base = _tmdb_image_base(api_key)
        poster_path = data.get("poster_path")
        poster_url = f"{image_base}w500{poster_path}" if poster_path else None

        # Resolve genres — TMDB detail endpoint returns genres as list of dicts
        # e.g. [{"id": 28, "name": "Action"}, ...]
        genres_raw = data.get("genres", [])
        genre_ids: list[int] = []
        genre_names: list[str] = []
        if genres_raw and isinstance(genres_raw, list) and isinstance(genres_raw[0], dict):
            genre_ids = [g["id"] for g in genres_raw if isinstance(g, dict) and "id" in g]
            genre_names = _resolve_genre_ids(genre_ids, api_key)

        origin_country = data.get("origin_country")
        result: dict[str, Any] = {
            "tmdb_id": data.get("id"),
            "media_type": media_type,
            "title_cn": data.get("name") or data.get("title"),
            "title_en": data.get("original_name") or data.get("original_title"),
            "overview": data.get("overview"),
            "poster_url": poster_url,
            "vote_average": data.get("vote_average"),
            "genre": genre_names,
            "status": data.get("status"),
            "original_language": data.get("original_language"),
            "origin_country": origin_country,
            # Deterministic anime verdict from genre+language/country; the
            # finalize prompt tells the LLM to copy this into matched_entity.
            "is_anime": is_anime_from_tmdb(
                genre_ids, data.get("original_language"), origin_country
            ),
        }

        if media_type == "tv":
            result["number_of_episodes"] = data.get("number_of_episodes")
            result["number_of_seasons"] = data.get("number_of_seasons")
            result["first_air_date"] = data.get("first_air_date")
            result["last_air_date"] = data.get("last_air_date")
            # Fetch season details
            seasons_raw = data.get("seasons", [])
            result["seasons"] = [
                {
                    "season_number": s.get("season_number"),
                    "episode_count": s.get("episode_count"),
                    "name": s.get("name"),
                }
                for s in seasons_raw
                if s.get("season_number", 0) > 0
            ]
        else:
            result["release_date"] = data.get("release_date")
            result["runtime"] = data.get("runtime")

        return {"success": True, "data": result}
    except Exception as e:
        logger.warning(
            "[metadata_agent] get_tmdb_details failed for tmdb_id=%s media_type=%s: %s",
            tmdb_id, media_type, e, exc_info=True,
        )
        return {"success": False, "data": {}, "error": str(e)}



async def fetch_tmdb_episode_list(
    tmdb_id: str | int, seasons: list[dict] | None
) -> list[dict] | None:
    """Fetch per-episode data for a TMDB TV work (P4, wikipedia symmetry).

    TMDB series details carry per-season counts only; episode titles/air dates
    need one ``GET /tv/{id}/season/{n}`` per season. Season 0 (specials) is
    skipped - it is not part of the main numbering. Per-season failures are
    tolerated (logged, that season's episodes omitted); the whole call returns
    None when no season yielded episodes, or when the work has more seasons
    than ``TMDB_EPISODE_FETCH_MAX_SEASONS`` (fan-out not worth it). Episodes
    are ``[{season, episode, title, air_date}]`` - the same shape the
    wikipedia parser produces and ``upsert_episodes`` consumes.
    """
    api_key = runtime_config.tmdb_api_key
    if not api_key:
        return None
    digits = re.sub(r"\D", "", str(tmdb_id))
    if not digits:
        return None
    season_numbers: set[int] = set()
    for s in seasons or []:
        if not isinstance(s, dict):
            continue
        try:
            n = int(s.get("season_number"))
        except (TypeError, ValueError):
            continue
        if n >= 1:
            season_numbers.add(n)
    season_list = sorted(season_numbers)
    if not season_list or len(season_list) > TMDB_EPISODE_FETCH_MAX_SEASONS:
        return None

    import asyncio

    import httpx

    sem = asyncio.Semaphore(TMDB_EPISODE_FETCH_CONCURRENCY)

    async def _one(client: Any, n: int) -> dict | None:
        async with sem:
            try:
                resp = await client.get(
                    f"https://api.themoviedb.org/3/tv/{digits}/season/{n}",
                    params={"api_key": api_key, "language": "zh-CN"},
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.warning(
                    "[metadata_agent] tmdb season fetch failed for tv/%s season %d: %s",
                    digits, n, e,
                )
                return None

    async with httpx.AsyncClient(timeout=15) as client:
        results = await asyncio.gather(*(_one(client, n) for n in season_list))

    episodes: list[dict] = []
    for n, data in zip(season_list, results):
        if not isinstance(data, dict):
            continue
        for ep in data.get("episodes") or []:
            num = ep.get("episode_number")
            if not isinstance(num, int) or num < 1:
                continue
            episodes.append({
                "season": n,
                "episode": num,
                "title": ep.get("name"),
                "air_date": ep.get("air_date") or None,
            })
    return episodes or None
