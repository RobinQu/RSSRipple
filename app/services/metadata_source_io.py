"""TMDB / Exa / Jina source I/O primitives.

Pure leaf module - no DB, no LLM. Extracted verbatim from metadata_agent.py
(Phase 1): the ``_execute_*`` HTTP wrappers that the LangGraph @tool layer
and the audio resolver call. TMDB genre/season resolution and Exa/Jina search
delegate to metadata_search_agent; TMDB details hit the TMDB API directly
using the configured api key.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.runtime_config import runtime_config

logger = logging.getLogger(__name__)


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
        genre_names: list[str] = []
        if genres_raw and isinstance(genres_raw, list) and isinstance(genres_raw[0], dict):
            genre_ids = [g["id"] for g in genres_raw if isinstance(g, dict) and "id" in g]
            genre_names = _resolve_genre_ids(genre_ids, api_key)

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



async def _execute_search_exa_agent(query: str) -> dict:
    """Search via Exa Agent as an independent web metadata source."""
    from app.services.metadata_search_agent import _search_exa

    try:
        logger.info("[metadata_agent][exa_tool] search_exa_agent query=%r", query[:200])
        results = await _search_exa(query)
        logger.info(
            "[metadata_agent][exa_tool] search_exa_agent done query=%r candidates=%d",
            query[:200], len(results),
        )
        return {"success": True, "data": results}
    except Exception as e:
        logger.warning(
            "[metadata_agent] search_exa_agent failed for query=%s: %s",
            query, e, exc_info=True,
        )
        return {"success": False, "data": [], "error": str(e)}


async def _execute_search_jina(query: str) -> dict:
    """Search the web via Jina Search (s.jina.ai) — SERP hits with full content."""
    from app.services.metadata_search_agent import _search_jina

    try:
        logger.info("[metadata_agent][jina_tool] search_jina query=%r", query[:200])
        results = await _search_jina(query)
        logger.info(
            "[metadata_agent][jina_tool] search_jina done query=%r hits=%d",
            query[:200], len(results),
        )
        return {"success": True, "data": results}
    except Exception as e:
        logger.warning(
            "[metadata_agent] search_jina failed for query=%s: %s",
            query, e, exc_info=True,
        )
        return {"success": False, "data": [], "error": str(e)}


async def _execute_read_jina_url(url: str, with_links: bool = False) -> dict:
    """Fetch a single URL's full content via Jina Reader (r.jina.ai)."""
    from app.services.metadata_search_agent import _read_jina_url

    try:
        logger.info("[metadata_agent][jina_tool] read_jina_url url=%r", url[:200])
        data = await _read_jina_url(url, with_links=with_links)
        if not data:
            return {"success": False, "data": {}, "error": "no content returned"}
        return {"success": True, "data": data}
    except Exception as e:
        logger.warning(
            "[metadata_agent] read_jina_url failed for url=%s: %s",
            url, e, exc_info=True,
        )
        return {"success": False, "data": {}, "error": str(e)}
