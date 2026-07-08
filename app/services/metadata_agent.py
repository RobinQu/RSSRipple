"""Unified ReAct metadata agent for RSS resource identification.

Replaces the old two-phase (title_cleaner → metadata_search_agent) pipeline
with a single LangGraph ReAct agent that:

1. Cleans the raw RSS title
2. Infers episode, season, and other resource fields
3. Searches exactly one selected metadata source: TMDB, Exa Agent, or Wikipedia
4. Uses the LLM to interpret that source's evidence
5. Returns a complete ``ResourceMetadata`` result

The agent builds one tool-restricted LangGraph graph per source, so source
selection is enforced by code rather than prompt wording alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent  # noqa: F401 — kept for compat; deprecation warning is harmless
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

DEFAULT_METADATA_SOURCE = "exa"
SUPPORTED_METADATA_SOURCES = {"tmdb", "exa", "wikipedia", "jina", "local"}

# User-selectable external metadata sources (ordered as presented in the UI).
# ``key`` is the credential attr on Settings; sources without a key
# (wikipedia) are considered configured whenever their enable switch is on.
_EXTERNAL_SOURCE_DEFS: tuple[dict[str, str], ...] = (
    {"value": "exa", "label": "Exa Agent", "key": "exa_api_key",
     "description": "Structured web-agent search; broad evidence coverage."},
    {"value": "jina", "label": "Jina Search + Reader", "key": "jina_api_key",
     "description": "Cheap web-native search with strong CJK coverage."},
    {"value": "wikipedia", "label": "Wikipedia", "key": "",
     "description": "Wikipedia REST search; no API key required."},
    {"value": "tmdb", "label": "TMDB", "key": "tmdb_api_key",
     "description": "The Movie Database; best for TV/movie ID matching."},
)


def is_metadata_source_configured(source: str) -> bool:
    """Whether the credentials for *source* are present (key set)."""
    for d in _EXTERNAL_SOURCE_DEFS:
        if d["value"] == source:
            return True if not d["key"] else bool(getattr(settings, d["key"], ""))
    return False


def is_metadata_source_enabled(source: str) -> bool:
    """Whether the enable switch for *source* is on."""
    flag = {
        "exa": settings.exa_enabled,
        "jina": settings.jina_enabled,
        "tmdb": settings.tmdb_enabled,
        "wikipedia": settings.wikipedia_enabled,
    }.get(source)
    return bool(flag)


def is_metadata_source_available(source: str) -> bool:
    """A source is an selectable candidate when enabled AND configured."""
    return is_metadata_source_enabled(source) and is_metadata_source_configured(source)


def get_metadata_source_catalog() -> list[dict[str, Any]]:
    """Return all external metadata sources with their availability flags.

    Each entry: ``{value, label, description, enabled, configured, available}``.
    The frontend offers only ``available`` sources in the channel form.
    """
    catalog: list[dict[str, Any]] = []
    for d in _EXTERNAL_SOURCE_DEFS:
        value = d["value"]
        catalog.append({
            "value": value,
            "label": d["label"],
            "description": d["description"],
            "enabled": is_metadata_source_enabled(value),
            "configured": is_metadata_source_configured(value),
            "available": is_metadata_source_available(value),
        })
    return catalog


def get_available_metadata_sources() -> list[dict[str, Any]]:
    """Return only the currently-selectable external metadata sources."""
    return [s for s in get_metadata_source_catalog() if s["available"]]


def resolve_metadata_source(value: str | None) -> str:
    """Resolve a channel's stored source to a runnable source.

    Returns the normalized source if it is supported, else the default. Callers
    that need an *available* source should additionally check
    :func:`is_metadata_source_available` and fall back.
    """
    return normalize_metadata_source_type(value)


def normalize_metadata_source_type(value: str | None) -> str:
    """Normalize a caller-provided metadata source.

    ``combined`` is accepted only as a legacy dataset value and maps to the
    default single source. ``local`` searches the in-app TVSeries/Movie library
    via FTS5 instead of calling an external API. New calls should pass
    tmdb/exa/wikipedia/local explicitly.
    """
    source = (value or DEFAULT_METADATA_SOURCE).strip().lower()
    if source == "combined":
        return DEFAULT_METADATA_SOURCE
    return source if source in SUPPORTED_METADATA_SOURCES else DEFAULT_METADATA_SOURCE

# ---------------------------------------------------------------------------
# Intermediate data type — sits between raw_title and DB entities
# ---------------------------------------------------------------------------


@dataclass
class ResourceMetadata:
    """Metadata extracted from a single RSS resource title.

    Independent of any DB entity. Used as the output of MetadataAgent
    in both production (applied to FileResource/TVSeries/Movie) and
    evaluation (compared against GroundTruth) flows.
    """

    # ── Core ──
    clean_title: str
    content_type: Literal["tv", "movie"] = "tv"
    found: bool = True

    # ── Inferred resource fields (subset of FileResource columns) ──
    title_cn: str | None = None
    title_en: str | None = None
    episode: int | None = None
    season: int | None = None
    # Multi-episode batch (合集). ``is_batch`` marks torrents containing many
    # episodes. ``episode_start`` / ``episode_end`` are best-effort — a batch
    # title may not spell out the boundaries (e.g. "Batch", "全集").
    is_batch: bool = False
    episode_start: int | None = None
    episode_end: int | None = None
    resolution: str | None = None
    source: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    subtitle_type: str | None = None
    subtitle_group: str | None = None
    # BCP-47 language tags: ["zh-CN", "zh-TW", "ja", "en"], or ["multi"] for
    # titles marked "多语言" / "多国字幕" without specifics. None means the
    # LLM had nothing to say — pre-parser output is kept.
    subtitle_langs: list[str] | None = None
    container: str | None = None

    # ── Matched entity metadata (upserted into TVSeries or Movie) ──
    matched_entity: dict | None = None
    # Keys: external_id, external_source, title_cn, title_en,
    #       original_title, description, poster_url, rating, genre,
    #       status, number_of_episodes, number_of_seasons,
    #       start_date, end_date, release_date, runtime,
    #       canonical_name, wikipedia_url

    # ── Quality ──
    confidence: float = 0.0
    reason: str | None = None

    # ── Ambiguity (for manual resolution) ──
    ambiguous: bool = False
    ambiguous_candidates: list[dict] = field(default_factory=list)

    # ── Search tracking (for eval) ──
    search_method: str | None = None
    data_sources_used: list[str] = field(default_factory=list)
    source_errors: dict[str, str] = field(default_factory=dict)
    search_error: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> ResourceMetadata:
        """Construct from the finalize tool's JSON output."""
        entity = data.get("matched_entity") or {}
        return cls(
            clean_title=data.get("clean_title", ""),
            content_type=data.get("content_type", "tv"),
            found=data.get("found", True),
            title_cn=data.get("title_cn") or entity.get("title_cn"),
            title_en=data.get("title_en") or entity.get("title_en"),
            episode=data.get("inferred_episode"),
            season=data.get("inferred_season"),
            is_batch=bool(data.get("is_batch", False)),
            episode_start=data.get("inferred_episode_start") or data.get("episode_start"),
            episode_end=data.get("inferred_episode_end") or data.get("episode_end"),
            resolution=data.get("resolution"),
            source=data.get("source"),
            video_codec=data.get("video_codec"),
            audio_codec=data.get("audio_codec"),
            subtitle_type=data.get("subtitle_type"),
            subtitle_group=data.get("subtitle_group"),
            subtitle_langs=data.get("subtitle_langs"),
            container=data.get("container"),
            matched_entity=entity if entity else None,
            confidence=float(data.get("confidence", 0.0)),
            reason=data.get("reason"),
            ambiguous=data.get("ambiguous", False),
            ambiguous_candidates=data.get("ambiguous_candidates", []),
            search_method=data.get("search_method"),
            data_sources_used=data.get("data_sources_used") or [],
            source_errors=data.get("source_errors") or {},
            search_error=data.get("search_error"),
        )


# ---------------------------------------------------------------------------
# Cross-season episode reconciliation
# ---------------------------------------------------------------------------

# Some RSS titles number episodes absolutely across all seasons (S04 - 84,
# where 84 = cumulative episode count across seasons 1-4) rather than
# per-season. We detect this by checking the raw episode against the
# season's episode_count from TMDB/Exa metadata and converting when the
# arithmetic works out. Values outside the tolerance envelope are flagged
# ``ambiguous`` and routed to AgentSuggestion for manual review.

# Extra headroom for still-airing shows where TMDB's episode_count lags a
# few episodes behind the true count.
_RECONCILE_TOLERANCE = 2


def _seasons_map_from(entity: dict | None) -> dict[int, int]:
    """Extract ``{season_number: episode_count}`` from a matched_entity dict.

    Both TMDB (native ``seasons``) and the Exa Agent schema (which mirrors
    it) return a list of season dicts. Season 0 = specials and is ignored.
    Returns an empty dict when there's no usable data.
    """
    if not isinstance(entity, dict):
        return {}
    seasons = entity.get("seasons")
    if not isinstance(seasons, list):
        return {}
    out: dict[int, int] = {}
    for s in seasons:
        if not isinstance(s, dict):
            continue
        num = s.get("season_number")
        cnt = s.get("episode_count")
        if not isinstance(num, int) or not isinstance(cnt, int):
            continue
        if num < 1 or cnt < 1:
            continue
        out[num] = cnt
    return out


def reconcile_episode(
    *,
    raw_episode: int,
    raw_season: int,
    seasons_map: dict[int, int],
) -> tuple[int, int | None, str] | None:
    """Decide whether ``raw_episode`` is per-season or absolute-across-seasons.

    Returns ``(episode, absolute_episode, confidence)`` where ``episode`` is
    the per-season number to store on the resource, ``absolute_episode`` is
    the audit value (or None when the raw was already per-season), and
    ``confidence`` is one of ``"raw" | "reconciled" | "ambiguous"``.

    Returns ``None`` when there's no basis to make a call — caller keeps
    the raw episode and (optionally) marks the resource ``"raw"``.

    Algorithm:
      * No entry for ``raw_season`` in ``seasons_map`` → return None. We
        can't tell.
      * ``raw_episode ≤ season_count + tolerance`` → it looks per-season;
        keep as-is (``confidence="raw"``).
      * Otherwise try converting: subtract the episode counts of prior
        seasons. If the candidate lands within ``[1, season_count]`` we
        accept the conversion (``confidence="reconciled"``). Otherwise
        return ``confidence="ambiguous"`` so the caller can route the
        resource to AgentSuggestion instead of dispatching.
    """
    season_count = seasons_map.get(raw_season)
    if season_count is None or season_count <= 0:
        return None

    # Case A — the raw number already looks like a per-season episode.
    if raw_episode <= season_count + _RECONCILE_TOLERANCE:
        return raw_episode, None, "raw"

    # Case B — try treating raw as absolute.
    prev_total = sum(
        cnt for s, cnt in seasons_map.items() if s < raw_season and cnt > 0
    )
    if prev_total <= 0:
        # Season 1 with a raw > season_count is just a strange release; leave
        # it ambiguous.
        return raw_episode, None, "ambiguous"

    candidate = raw_episode - prev_total
    if 1 <= candidate <= season_count + _RECONCILE_TOLERANCE:
        # Clamp to season_count when tolerance overshoots — TMDB just being
        # behind on episode_count is the common case.
        final_ep = min(candidate, season_count) if candidate > season_count else candidate
        return final_ep, raw_episode, "reconciled"

    return raw_episode, None, "ambiguous"


# ---------------------------------------------------------------------------
# Tool backing implementations
# ---------------------------------------------------------------------------


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

    api_key = settings.tmdb_api_key
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


async def _execute_search_wikipedia(query: str, lang: str = "en") -> dict:
    """Search Wikipedia for matching pages."""
    try:
        import wikipedia

        wiki_lang = lang if lang in ("en", "zh", "ja") else "en"
        wikipedia.set_lang(wiki_lang)

        results = await asyncio.to_thread(wikipedia.search, query, results=5)
        if not results:
            return {"success": True, "data": []}

        pages = []
        for title in results[:5]:
            try:
                page = await asyncio.to_thread(wikipedia.page, title, auto_suggest=False)
                pages.append(
                    {
                        "title": page.title,
                        "page_id": page.pageid,
                        "url": page.url,
                        "summary": page.summary[:500] if page.summary else "",
                    }
                )
            except (wikipedia.exceptions.DisambiguationError, wikipedia.exceptions.PageError):
                continue
        return {"success": True, "data": pages}
    except Exception as e:
        logger.warning(
            "[metadata_agent] search_wikipedia failed for query=%s lang=%s: %s",
            query, lang, e, exc_info=True,
        )
        return {"success": False, "data": [], "error": str(e)}


async def _execute_get_wikipedia_page(title: str, lang: str = "en") -> dict:
    """Get full Wikipedia page with infobox and categories."""
    try:
        import wikipedia

        wiki_lang = lang if lang in ("en", "zh", "ja") else "en"
        wikipedia.set_lang(wiki_lang)

        page = await asyncio.to_thread(wikipedia.page, title, auto_suggest=False)

        return {
            "success": True,
            "data": {
                "title": page.title,
                "page_id": page.pageid,
                "url": page.url,
                "summary": page.summary[:800] if page.summary else "",
                "categories": page.categories[:20] if page.categories else [],
            },
        }
    except wikipedia.exceptions.DisambiguationError as e:
        return {
            "success": True,
            "data": {
                "title": title,
                "disambiguation": True,
                "options": e.options[:10] if e.options else [],
            },
        }
    except wikipedia.exceptions.PageError:
        return {"success": False, "data": {}, "error": f"Page not found: {title}"}
    except Exception as e:
        logger.warning(
            "[metadata_agent] get_wikipedia_page failed for title=%s lang=%s: %s",
            title, lang, e, exc_info=True,
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


# ---------------------------------------------------------------------------
# LangChain tools
# ---------------------------------------------------------------------------


@tool
async def search_tmdb(query: str) -> str:
    """Search TMDB API for TV shows and movies.

    Use this in TMDB source mode to find candidate works. Returns candidates sorted by rating.
    For anime, try Japanese romanized title. For Western shows, use English.

    Args:
        query: Search query string (optimize for TMDB: English or romanized Japanese)

    Returns:
        JSON: {"success": true, "data": [{tmdb_id, media_type, title_cn, title_en,
        original_title, year, overview, rating, poster_path, genre}]}
    """
    result = await _execute_search_tmdb(query)
    return json.dumps(result, ensure_ascii=False)


@tool
async def get_tmdb_details(tmdb_id: str, media_type: str) -> str:
    """Get full TMDB details including episode/season structure.

    Use when you need to verify season numbers, episode counts, or status.
    Essential for resolving which season an episode belongs to.

    Args:
        tmdb_id: TMDB ID (integer as string, e.g. "85937")
        media_type: "tv" or "movie"

    Returns:
        JSON: {success, data: {number_of_episodes, number_of_seasons, status, genre,
        seasons: [{season_number, episode_count, name}], poster_url, first_air_date, ...}}
    """
    result = await _execute_get_tmdb_details(tmdb_id, media_type)
    return json.dumps(result, ensure_ascii=False)


@tool
async def search_wikipedia(query: str, lang: str = "en") -> str:
    """Search Wikipedia for pages matching the query.

    Use this in Wikipedia source mode to search Wikipedia directly.
    Use lang="zh" for Chinese titles, "ja" for Japanese.

    Args:
        query: Search query
        lang: Language code: "en", "zh", "ja" (default "en")

    Returns:
        JSON: {"success": true, "data": [{title, page_id, url, summary}]}
    """
    result = await _execute_search_wikipedia(query, lang)
    return json.dumps(result, ensure_ascii=False)


@tool
async def get_wikipedia_page(title: str, lang: str = "en") -> str:
    """Get full Wikipedia page content with categories.

    Use to extract the canonical name of a work and verify its type.
    Categories help determine if something is a TV series vs film vs anime.

    Args:
        title: Exact Wikipedia page title (from search_wikipedia results)
        lang: Language code: "en", "zh", "ja" (default "en")

    Returns:
        JSON: {success, data: {title, page_id, url, summary, categories}}
    """
    result = await _execute_get_wikipedia_page(title, lang)
    return json.dumps(result, ensure_ascii=False)


@tool
async def search_exa_agent(query: str) -> str:
    """Search Exa Agent for structured web metadata about a work.

    This tool is available only in Exa source mode.

    Args:
        query: Search query

    Returns:
        JSON: {"success": true, "data": [{content_type, title_cn, title_en,
        original_title, description, external_id, external_source, ...}]}
    """
    result = await _execute_search_exa_agent(query)
    return json.dumps(result, ensure_ascii=False)


@tool
async def search_jina(query: str) -> str:
    """Search the web via Jina Search for pages about a work.

    Available only in Jina source mode. Returns SERP hits, each with the full
    markdown ``content`` of the top pages — scan titles/URLs for the work, then
    read the content for canonical names, years, and external IDs. Prefer
    authoritative URLs: TMDB, IMDb, Wikipedia, Wikidata, Fandom, MyAnimeList,
    AniList. If the best URL was not in the top results, call ``read_jina_url``
    on it to fetch its content directly.

    Args:
        query: Search query (try Chinese, romanized Japanese, or English variants)

    Returns:
        JSON: {"success": true, "data": [{title, url, description, content}]}
    """
    result = await _execute_search_jina(query)
    return json.dumps(result, ensure_ascii=False)


@tool
async def read_jina_url(url: str) -> str:
    """Fetch a single URL's full content via Jina Reader.

    Use in Jina source mode when ``search_jina`` did not surface a promising
    page, or to read a specific TMDB/IMDb/Wikipedia URL in full. Returns the
    page's markdown content; extract the canonical title, year, external ID,
    and poster URL from it.

    Args:
        url: Absolute URL to read (e.g. a TMDB/IMDb/Wikipedia page URL)

    Returns:
        JSON: {"success": true, "data": {title, url, description, content, links}}
    """
    result = await _execute_read_jina_url(url)
    return json.dumps(result, ensure_ascii=False)


@tool
def finalize(result_json: str) -> str:
    """Submit the final metadata result. ALWAYS call this to end the task.

    Call when you have identified the work OR confirmed no match exists.

    Args:
        result_json: JSON string matching this schema:
          Required: found(bool), clean_title(str), content_type("tv"|"movie")
          When found=true: matched_entity with at minimum external_id, title_cn, title_en
          When found=false: reason(str)
          Optional: inferred_episode(int), inferred_season(int), inferred_fields,
            ambiguous(bool), ambiguous_candidates(list), confidence(float)

    Returns:
        "FINALIZED"
    """
    return "FINALIZED"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """You are a metadata agent for anime/TV/movie RSS feeds. Your job:
Given a raw RSS entry title, identify the work (TV series or movie), extract its
canonical clean title, infer episode/season numbers from the title, and return
structured metadata via the finalize tool.

## FEW-SHOT EXAMPLES

Example 1 — Chinese anime with season number in brackets and title:
  Raw: "[SweetSub&LoliHouse] 小书痴的下克上 领主的养女 / Honzuki no Gekokujou S04 - 11 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]（第四季）"
  → clean_title: "小书痴的下克上 领主的养女"
  → content_type: tv, episode: 11, season: 4
  → subtitle_group: "SweetSub&LoliHouse", resolution: "1080p"
  → subtitle_langs: ["zh-CN", "zh-TW", "ja"]
  → title_cn: "小书痴的下克上 领主的养女", title_en: "Ascendance of a Bookworm"
  → search query: "Ascendance of a Bookworm"

Example 2 — English TV with SXXEXX notation:
  Raw: "Ace Of The Diamond S04E13 720p WEB H264-SKYANiME"
  → clean_title: "Ace of the Diamond", content_type: tv, episode: 13, season: 4
  → title_en: "Ace of the Diamond", resolution: "720p", source: "WEB"
  → video_codec: "H264", subtitle_group: "SKYANiME"

Example 3 — Anime with season number embedded in title:
  Raw: "[LoliHouse] 异世界悠闲农家 2 / Isekai Nonbiri Nouka 2 - 12 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
  → clean_title: "异世界悠闲农家", content_type: tv, episode: 12, season: 2
  → title_cn: "异世界悠闲农家", title_en: "Farming Life in Another World"

Example 4 — No recognizable work:
  Raw: "random_bytes_xyz123 1080p"
  → found: false, reason: "No matching work found in the selected source"

Example 5 — Multi-episode batch with explicit range:
  Raw: "魔法帽的工作室「とんがり帽子のアトリエ」Witch Hat Atelier S01E01~13 1080p 多国字幕"
  → clean_title: "Witch Hat Atelier"
  → content_type: tv, season: 1, episode: null
  → is_batch: true, inferred_episode_start: 1, inferred_episode_end: 13
  → resolution: "1080p"

Example 6 — Chinese collection tag with "合集":
  Raw: "[LoliHouse] 异世界悠闲农家 2 / Isekai Nonbiri Nouka 2 [01-12 合集][WebRip 1080p HEVC-10bit AAC][简繁内封字幕][Fin]"
  → clean_title: "异世界悠闲农家", content_type: tv, season: 2, episode: null
  → is_batch: true, inferred_episode_start: 1, inferred_episode_end: 12
  → subtitle_group: "LoliHouse", resolution: "1080p"

Example 7 — Batch without explicit boundaries:
  Raw: "[SubGroup] Some Show S02 (Season Pack) 1080p"
  → is_batch: true, inferred_episode_start: null, inferred_episode_end: null
  → episode: null, season: 2

## RULES

1. Use only the metadata source selected by the caller. The available tools
   in this run already enforce that choice.
2. Do not try to compensate for missing evidence by switching to another
   source. If the selected source fails, finalize with found=false.
3. Use the LLM to interpret the selected source evidence and produce one final
   judgment.
4. If the selected source fails → finalize with found=false.
5. Do NOT call the same tool with the same parameters more than once.
6. ALWAYS call finalize to end. Never leave a task unfinished.

## TITLE PARSING

From raw RSS titles, extract:
- Clean title: remove [subtitle groups], - episode numbers, [quality/codec tags]
- Episode: from "- 05", "EP05", "#05", "第05话", "S04E05" → the second number
- Season: from "第二季", "Season 2", "S2", "S02", "II", "Ⅲ", "Final Season",
  "S04" (when SXXEXX format), parenthetical like "（第四季）"
- Season arcs: "游郭篇", "无限列车篇", "领主的养女" often indicate specific seasons
- Batch detection: set ``is_batch: true`` (and leave ``inferred_episode`` null)
  when the title covers multiple episodes:
  * ``SxxE01~13``, ``SxxE01-13`` (episode range)
  * ``[01-12 合集]``, ``[01~16 Fin]``, ``01-12 合集``
  * ``Season Pack``, ``Full Season``, ``Batch``, ``BD-BOX``
  * ``全集``, ``全季``, ``完整`` / ``完结`` + range
  Fill ``inferred_episode_start`` / ``inferred_episode_end`` when the boundaries
  are stated; leave them null when the title only says "Batch" / "全集".
- Quality: resolution (1080p/720p/2160p/4K), source (WebRip/WEB-DL/BDRip),
  codecs (HEVC/AVC/x264/x265, AAC/FLAC), subtitle types, container (MKV/MP4)
- Subtitle languages: emit ``subtitle_langs`` as a list of BCP-47 tags —
  ``"zh-CN"`` for 简中/CHS/简体/GB, ``"zh-TW"`` for 繁中/CHT/繁體/BIG5,
  ``"ja"`` for 日文/JAP/Japanese, ``"en"`` for 英文/ENG/English. Use the
  sentinel ``"multi"`` (and nothing else) when the title only says
  "多语言" / "多国字幕" / "Multi-Sub" without spelling out which languages.
  Emit ``[]`` when the title has no subtitle marker at all; only use
  ``null`` to mean "I don't know / defer to the pre-parser".

## SEARCH QUERY VARIANTS (Jina mode only)

When the title spans multiple languages (Chinese/Japanese/English), try these
variants in order and combine evidence across them:
  1. Chinese title (title_cn) — best for Chinese release info, Baidu/Douban
  2. Romanized Japanese — for anime, use the romaji title
  3. English title — for TMDB/IMDb-style databases
Search each with ``search_jina`` at most once. Prefer TMDB / IMDb / Wikipedia /
Wikidata / MyAnimeList / AniList URLs in the results.

## SOURCE MODE
- TMDB mode: use search_tmdb and get_tmdb_details only.
- Exa mode: use search_exa_agent only.
- Wikipedia mode: use search_wikipedia and get_wikipedia_page only.
- Jina mode: use search_jina and read_jina_url only. Cap at 3 tool calls before
  finalize. When evidence comes from a TMDB/IMDb page reached via Jina, emit
  external_id in canonical form (tmdb:XXXXX / imdb:ttXXXXXXX) — Jina is the
  route, TMDB/IMDb is the identifier source.

## finalize SCHEMA
Always output valid JSON matching:
{
  "found": true/false,
  "clean_title": "string",
  "content_type": "tv"|"movie",
  "inferred_episode": int|null,
  "inferred_season": int|null,
  "is_batch": true/false,
  "inferred_episode_start": int|null,
  "inferred_episode_end": int|null,
  "title_cn": "string|null",
  "title_en": "string|null",
  "subtitle_group": "string|null",
  "resolution": "string|null",
  "source": "string|null",
  "video_codec": "string|null",
  "audio_codec": "string|null",
  "subtitle_type": "string|null",
  "subtitle_langs": ["zh-CN"|"zh-TW"|"ja"|"en"|"multi", ...] | null,
  "container": "string|null",
  "matched_entity": {
    "external_id": "tmdb:XXXXX",
    "external_source": "tmdb",  # tmdb|exa|wikipedia|jina — canonical ID source
    "title_cn": "...", "title_en": "...", "original_title": "...",
    "description": "...", "poster_url": "...",
    "rating": float, "genre": [...],
    "status": "...", "number_of_episodes": int, "number_of_seasons": int,
    "seasons": [
      {"season_number": 1, "episode_count": 24, "name": "Season 1"},
      {"season_number": 2, "episode_count": 24}
    ],
    "start_date": "YYYY-MM-DD", "canonical_name": "...", "wikipedia_url": "..."
  } | null,
  "ambiguous": true/false,
  "ambiguous_candidates": [],
  "data_sources_used": ["tmdb"|"exa"|"wikipedia"|"jina"],
  "confidence": 0.0-1.0,
  "reason": "explanation"
}
"""


# ---------------------------------------------------------------------------
# Failure classification + attempt recording
#
# ``process()`` used to cache every result — including ``found=false`` from
# timeouts and LLM-format errors — so a transient failure became a permanent
# "not found". These helpers split non-success results into three buckets so
# the cache only retains *definitive* outcomes and the fetch-time backfill
# knows which unmatched resources are worth retrying.
# ---------------------------------------------------------------------------

# Substrings of ``ResourceMetadata.reason`` / ``search_error`` that indicate
# an infra failure (not a real "no match"). These must NOT be cached, because
# re-running later will very likely succeed.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "timed out", "timeout", "connection error", "did not call finalize",
    "403", "accountoverdue", "api key not configured",
    "rate limit", "service unavailable", "overloaded",
)

# Substrings indicating the entry is genuinely not a TV/movie work (music,
# ASMR, theme songs). Re-running will not change the outcome.
_NON_WORK_MARKERS: tuple[str, ...] = (
    "music album", "music single", "music release", "mini-album", "mini album",
    "asmr", "opening theme", "ending theme", "theme song",
    "not a tv", "not a movie", "not an anime",
)


def _classify_failure(meta: Any) -> str | None:
    """Classify a ``ResourceMetadata`` outcome for retry/cache decisions.

    Returns ``None`` on success (``meta.found`` truthy). Otherwise one of:
      * ``"transient"``  — retryable infra failure; never cached.
      * ``"non_work"``   — correctly identified as non-TV/movie; never retried.
      * ``"not_found"``  — source had no match; retried after a long TTL.
    """
    if getattr(meta, "found", False):
        return None
    haystack = " ".join(filter(None, (
        str(getattr(meta, "reason", "") or ""),
        str(getattr(meta, "search_error", "") or ""),
    ))).lower()
    if any(m in haystack for m in _TRANSIENT_MARKERS):
        return "transient"
    if any(m in haystack for m in _NON_WORK_MARKERS):
        return "non_work"
    return "not_found"


def _record_metadata_attempt(resource: Any, meta: Any) -> None:
    """Stamp retry-state columns on ``resource`` after an evaluation.

    ``metadata_matched_at`` only records successes, so this tracks *attempts*
    (count + timestamp + failure type) so the backfill can tell "never tried"
    from "tried and failed transiently" from "definitively not found".
    ``metadata_failure_type`` is set to ``None`` on success, which also clears
    any stale failure marker left by a previous attempt.
    """
    resource.metadata_attempts = int(getattr(resource, "metadata_attempts", 0) or 0) + 1
    resource.last_metadata_attempt_at = utcnow()
    resource.metadata_failure_type = _classify_failure(meta)


def _cache_source_key(data_source_type: str | None) -> str:
    """Cache namespace for one metadata source.

    The cache is keyed by ``(title, source)`` where ``source`` carries both the
    cache type and the data source, e.g. ``"metadata_agent:jina"``. This keeps
    results from one source (e.g. Exa) from being returned for a channel
    configured with another (e.g. Jina) - switching a channel's source no
    longer serves stale results from the old source.
    """
    ns = normalize_metadata_source_type(data_source_type)
    return f"metadata_agent:{ns}"


# ---------------------------------------------------------------------------
# Main agent class
# ---------------------------------------------------------------------------


class UnifiedMetadataAgent:
    """ReAct metadata agent backed by LangGraph.

    Usage:
        agent = UnifiedMetadataAgent()

        # Production: process a FileResource (writes to DB)
        await agent.process(resource, channel, db)

        # Eval/testing: stateless title-only extraction
        result: ResourceMetadata = await agent.process_title_only(raw_title)
    """

    MAX_LANGGRAPH_RECURSION_LIMIT: ClassVar[int] = 45

    def __init__(self) -> None:
        self._model = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            temperature=0.1,
            timeout=30,
            max_retries=1,
        )
        self._agents: dict[str, Any] = {}

    def _tools_for_source(self, data_source_type: str) -> list[Any]:
        """Return the exact tool surface for one metadata data source."""
        source = normalize_metadata_source_type(data_source_type)
        if source == "tmdb":
            return [search_tmdb, get_tmdb_details, finalize]
        if source == "wikipedia":
            return [search_wikipedia, get_wikipedia_page, finalize]
        if source == "jina":
            return [search_jina, read_jina_url, finalize]
        return [search_exa_agent, finalize]

    def _agent_for_source(self, data_source_type: str) -> Any:
        """Lazily build a ReAct graph whose tools are limited to one source."""
        source = normalize_metadata_source_type(data_source_type)
        if source not in self._agents:
            self._agents[source] = create_react_agent(
                model=self._model,
                tools=self._tools_for_source(source),
                prompt=_SYSTEM_PROMPT,
            )
        return self._agents[source]

    # ── Production entry ──

    async def process(
        self,
        resource: Any,
        channel: Any,
        db: AsyncSession,
        force_refresh: bool = False,
    ) -> ResourceMetadata | None:
        """Process a FileResource: extract metadata and persist to DB.

        Writes search_title, episode, season, series_id/movie_id to the
        FileResource. Upserts TVSeries or Movie as needed. Caches result
        in MetadataCache.

        ``force_refresh`` skips the cache *read* so retry-eligible resources
        re-run the agent live even when a (possibly stale or transient-failure)
        cache entry exists. Transient failures are never written to the cache,
        so a timeout/LLM-format error can no longer poison future runs.
        """
        raw_title = getattr(resource, "title_raw", "") or ""
        if not raw_title.strip():
            return None

        # Resolve the channel's data source up front so the cache lookup is
        # source-scoped (a Jina channel must not hit a stale Exa cache entry).
        data_source_type = resolve_metadata_source(getattr(channel, "metadata_source", None))

        # 0. Cache check — skipped on force_refresh. Legacy cache rows that
        # recorded a *transient* failure (timeout / "did not call finalize")
        # are also ignored and re-run live, since the cached outcome is not
        # trustworthy. Definitive results (found / not_found / non_work) are
        # applied directly without spending another LLM call.
        cached: ResourceMetadata | None = None
        if not force_refresh:
            cached = await self._get_cache(raw_title, data_source_type, db)
            if cached is not None and _classify_failure(cached) != "transient":
                await self._apply_to_resource(cached, resource, channel, db)
                _record_metadata_attempt(resource, cached)
                return cached

        # 1. Build context - if the chosen source's credentials are
        # missing/disabled, we still run its graph (the per-source search helper
        # no-ops on missing keys) but log a warning so it is debuggable.
        if not is_metadata_source_available(data_source_type) and data_source_type != "local":
            logger.warning(
                "[metadata_agent] channel %s source=%r is not available (disabled or "
                "missing credentials); search will return no external candidates",
                getattr(channel, "id", "?"), data_source_type,
            )
        message = self._build_production_message(resource, channel, data_source_type)

        # 2. Run ReAct
        finalize_dict, search_info = await self._run_react(message, data_source_type)
        finalize_dict["search_method"] = search_info.get("method")
        finalize_dict["data_sources_used"] = search_info.get("data_sources_used") or []
        finalize_dict["source_errors"] = search_info.get("source_errors") or {}
        finalize_dict["search_error"] = search_info.get("error")

        # 3. Parse
        meta = ResourceMetadata.from_dict(finalize_dict)

        # Default season to 1 for TV when not inferable
        if meta.content_type == "tv" and meta.season is None and meta.found:
            meta.season = 1

        # 4. Persist — record the attempt (success or failure) and cache only
        # definitive outcomes. Transient failures are intentionally NOT cached
        # so the next fetch's backfill retries them.
        await self._apply_to_resource(meta, resource, channel, db)
        _record_metadata_attempt(resource, meta)
        if _classify_failure(meta) != "transient":
            await self._set_cache(raw_title, data_source_type, meta, db)

        return meta

    # ── Eval/testing entry ──

    async def process_title_only(
        self,
        raw_title: str,
        data_source_type: str | None = None,
    ) -> ResourceMetadata:
        """Stateless, DB-free extraction for evaluation/testing.

        Does NOT read/write any DB entity. Returns ResourceMetadata directly.
        """
        if not raw_title.strip():
            return ResourceMetadata(clean_title="", found=False, reason="Empty title")

        if not settings.llm_api_key:
            return ResourceMetadata(
                clean_title=raw_title.strip()[:100],
                found=False,
                reason="LLM API key not configured",
            )

        source = normalize_metadata_source_type(data_source_type)
        logger.info("[metadata_agent] process_title_only source=%s title=%r", source, raw_title[:200])
        message = self._build_title_only_message(raw_title, source)
        finalize_dict, search_info = await self._run_react(message, source)
        finalize_dict["search_method"] = search_info.get("method")
        finalize_dict["data_sources_used"] = search_info.get("data_sources_used") or []
        finalize_dict["source_errors"] = search_info.get("source_errors") or {}
        finalize_dict["search_error"] = search_info.get("error")
        meta = ResourceMetadata.from_dict(finalize_dict)

        # Default season to 1 for TV when not inferable
        if meta.content_type == "tv" and meta.season is None and meta.found:
            meta.season = 1

        return meta

    # ── Message builders ──

    def _build_title_only_message(
        self,
        raw_title: str,
        data_source_type: str | None = None,
    ) -> str:
        source = normalize_metadata_source_type(data_source_type)
        source_guidance = {
            "tmdb": (
                "Source mode: TMDB Search. Use TMDB metadata only."
            ),
            "exa": (
                "Source mode: Exa Agent Search. Use Exa Agent metadata only."
            ),
            "wikipedia": (
                "Source mode: Wikipedia Search. Use Wikipedia metadata only."
            ),
            "jina": (
                "Source mode: Jina Search + Reader. Use search_jina to find pages, "
                "read_jina_url to fetch a specific page in full. Prefer TMDB / IMDb / "
                "Wikipedia / Wikidata / Fandom / MyAnimeList URLs. Cap of 3 tool calls "
                "before finalize. When the evidence references a TMDB or IMDb page, emit "
                "external_id as tmdb:XXXXX / imdb:ttXXXXXXX (Jina is the route, TMDB/IMDb "
                "the identifier source)."
            ),
        }[source]
        return f"{source_guidance}\n\nAnalyze this RSS entry title:\n\n{raw_title}"

    def _build_production_message(
        self,
        resource: Any,
        channel: Any,
        data_source_type: str = DEFAULT_METADATA_SOURCE,
    ) -> str:
        raw = getattr(resource, "title_raw", "")
        source = normalize_metadata_source_type(data_source_type)
        parts = [
            f"Source mode: {source}. Use only this selected metadata source.",
            f"Analyze this RSS entry title:\n\n{raw}",
        ]

        # Add pre-parsed fields as hints
        hints = []
        for attr in (
            "title_cn", "title_en", "subtitle_group", "episode", "season",
            "resolution", "source", "video_codec", "audio_codec",
            "subtitle_type", "container",
        ):
            val = getattr(resource, attr, None)
            if val is not None:
                hints.append(f"  {attr}: {val}")
        if hints:
            parts.append("\nPre-parsed fields (from field_mapping, may be unreliable):")
            parts.extend(hints)

        parts.append(
            f"\nChannel: {getattr(channel, 'name', 'unknown')}"
        )

        return "\n".join(parts)

    # ── ReAct execution ──

    async def _run_react(
        self,
        user_message: str,
        data_source_type: str | None = None,
    ) -> tuple[dict, dict]:
        """Execute the ReAct loop and return (finalize_dict, search_info)."""
        config = {"recursion_limit": self.MAX_LANGGRAPH_RECURSION_LIMIT}
        source = normalize_metadata_source_type(data_source_type)
        try:
            logger.info("[metadata_agent] ReAct start source=%s", source)
            result = await self._agent_for_source(source).ainvoke(
                {"messages": [HumanMessage(content=user_message)]},
                config=config,
            )
            logger.info("[metadata_agent] ReAct done source=%s messages=%d", source, len(result.get("messages", [])))
        except Exception as e:
            logger.error("[metadata_agent] ReAct invocation failed: %s", e, exc_info=True)
            return (
                {
                    "found": False,
                    "clean_title": "",
                    "content_type": "tv",
                    "reason": f"Agent error: {e}",
                },
                {"method": None, "data_sources_used": [source], "error": str(e)},
            )

        messages = result.get("messages", [])
        return (
            self._extract_finalize_result(messages),
            self._extract_search_info(messages),
        )

    def _extract_finalize_result(self, messages: list) -> dict:
        """Extract the JSON payload from the finalize tool call."""
        from langchain_core.messages import AIMessage, ToolMessage

        # Walk backwards to find the last finalize call
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.get("name") == "finalize":
                        try:
                            return json.loads(tc["args"].get("result_json", "{}"))
                        except json.JSONDecodeError:
                            pass

        # Fallback: check ToolMessages
        for msg in reversed(messages):
            if isinstance(msg, ToolMessage) and msg.name == "finalize":
                try:
                    inner = json.loads(msg.content)
                    if isinstance(inner, dict):
                        return inner
                except json.JSONDecodeError:
                    pass

        logger.warning("[metadata_agent] No finalize call found in agent messages")
        return {"found": False, "clean_title": "", "content_type": "tv", "reason": "Agent did not call finalize"}

    @staticmethod
    def _extract_search_info(messages: list) -> dict:
        """Inspect ReAct messages to determine which search tools were used and their outcome."""
        from langchain_core.messages import AIMessage, ToolMessage

        methods_used: set[str] = set()
        source_errors: dict[str, str] = {}
        search_error: str | None = None

        for msg in messages:
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.get("name", "")
                    if name == "search_tmdb":
                        methods_used.add("tmdb")
                    elif name == "get_tmdb_details":
                        methods_used.add("tmdb")
                    elif name == "search_exa_agent":
                        methods_used.add("exa")
                    elif name == "search_wikipedia":
                        methods_used.add("wikipedia")
                    elif name == "get_wikipedia_page":
                        methods_used.add("wikipedia")
                    elif name == "search_jina":
                        methods_used.add("jina")
                    elif name == "read_jina_url":
                        methods_used.add("jina")
            elif isinstance(msg, ToolMessage):
                if msg.name == "search_tmdb":
                    try:
                        content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                        if isinstance(content, dict):
                            if not content.get("success"):
                                source_errors.setdefault("tmdb", content.get("error", "no results"))
                                search_error = search_error or f"TMDB: {content.get('error', 'no results')}"
                            elif not content.get("data"):
                                source_errors.setdefault("tmdb", "no results")
                                search_error = search_error or "TMDB: no results"
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif msg.name == "get_tmdb_details":
                    try:
                        content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                        if isinstance(content, dict) and not content.get("success"):
                            source_errors.setdefault("tmdb", content.get("error", "details failed"))
                            search_error = search_error or f"TMDB details: {content.get('error', 'failed')}"
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif msg.name == "search_exa_agent":
                    try:
                        content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                        if isinstance(content, dict):
                            if not content.get("success"):
                                source_errors.setdefault("exa", content.get("error", "no results"))
                                search_error = search_error or f"Exa: {content.get('error', 'no results')}"
                            elif not content.get("data"):
                                source_errors.setdefault("exa", "no results")
                                search_error = search_error or "Exa: no results"
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif msg.name in ("search_wikipedia", "get_wikipedia_page"):
                    try:
                        content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                        if isinstance(content, dict):
                            if not content.get("success"):
                                source_errors.setdefault("wikipedia", content.get("error", "no results"))
                            elif msg.name == "search_wikipedia" and not content.get("data"):
                                source_errors.setdefault("wikipedia", "no results")
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif msg.name in ("search_jina", "read_jina_url"):
                    try:
                        content = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                        if isinstance(content, dict):
                            if not content.get("success"):
                                source_errors.setdefault("jina", content.get("error", "no results"))
                                search_error = search_error or f"Jina: {content.get('error', 'no results')}"
                            elif msg.name == "search_jina" and not content.get("data"):
                                source_errors.setdefault("jina", "no results")
                                search_error = search_error or "Jina: no results"
                    except (json.JSONDecodeError, TypeError):
                        pass

        return {
            "method": "|".join(sorted(methods_used)) if methods_used else None,
            "data_sources_used": sorted(methods_used),
            "source_errors": source_errors,
            "error": search_error,
        }

    # ── Persistence ──

    async def _apply_to_resource(
        self,
        meta: ResourceMetadata,
        resource: Any,
        channel: Any,
        db: AsyncSession,
    ) -> None:
        """Write metadata results back to the FileResource and DB."""
        resource.search_title = meta.clean_title

        if meta.found and meta.content_type == "tv":
            if meta.episode is not None:
                resource.episode = resource.episode or meta.episode
            if meta.season is not None:
                resource.season = resource.season or meta.season
        # Batch info — LLM output overrides pre-parser only when non-null.
        if meta.is_batch:
            resource.is_batch = True
        if meta.episode_start is not None:
            resource.episode_start = meta.episode_start
        if meta.episode_end is not None:
            resource.episode_end = meta.episode_end
        # A batch resource must not carry a stray single ``episode`` — that
        # would confuse downstream dedup logic. Clear it if the LLM committed.
        if resource.is_batch:
            resource.episode = None
        if meta.title_cn:
            resource.title_cn = resource.title_cn or meta.title_cn
        if meta.title_en:
            resource.title_en = resource.title_en or meta.title_en
        # LLM output overrides pre-parser only when it actually returned
        # something. ``[]`` is treated as "LLM saw no marker either", still
        # useful signal — keep it.
        if meta.subtitle_langs is not None:
            resource.subtitle_langs = list(meta.subtitle_langs)

        # Cross-season episode reconciliation. Runs on single-episode TV
        # resources only — batches are aggregated ranges and movies don't
        # carry an episode number. The pre-parser's NN(MM) hit is already
        # recorded on the resource (episode_confidence == "reconciled");
        # skip further work when that ran.
        if (
            meta.found
            and meta.content_type == "tv"
            and not resource.is_batch
            and resource.episode is not None
            and resource.season is not None
            and getattr(resource, "episode_confidence", None) not in ("manual", "reconciled")
        ):
            seasons_map = _seasons_map_from(meta.matched_entity)
            reconciled = reconcile_episode(
                raw_episode=resource.episode,
                raw_season=resource.season,
                seasons_map=seasons_map,
            )
            if reconciled is not None:
                new_episode, abs_ep, confidence = reconciled
                resource.episode = new_episode
                if abs_ep is not None:
                    resource.absolute_episode = abs_ep
                resource.episode_confidence = confidence
            elif getattr(resource, "episode_confidence", None) is None:
                # No seasons_map / no basis to reconcile — mark as raw so
                # downstream code can distinguish "never reconciled" from
                # "reconciled and unchanged".
                resource.episode_confidence = "raw"

        # Link to TVSeries or Movie
        if meta.found and meta.matched_entity:
            from app.services.metadata_service import (
                create_or_update_movie_from_external,
                create_or_update_series_from_external,
            )

            if meta.content_type == "movie":
                movie = await create_or_update_movie_from_external(db, meta.matched_entity)
                resource.movie_id = movie.id
                resource.series_id = None
            else:
                series = await create_or_update_series_from_external(db, meta.matched_entity)
                resource.series_id = series.id
                resource.movie_id = None

            resource.metadata_matched_at = utcnow()

    # ── Cache ──

    async def _get_cache(
        self, raw_title: str, data_source_type: str | None, db: AsyncSession
    ) -> ResourceMetadata | None:
        from sqlalchemy import select

        from app.models.metadata_cache import MetadataCache

        source_key = _cache_source_key(data_source_type)
        result = await db.execute(
            select(MetadataCache).where(
                MetadataCache.title == raw_title.strip(),
                MetadataCache.source == source_key,
            )
        )
        cached = result.scalar_one_or_none()
        if cached and isinstance(cached.metadata_json, dict):
            return ResourceMetadata.from_dict(cached.metadata_json)
        return None

    async def _set_cache(
        self, raw_title: str, data_source_type: str | None, meta: ResourceMetadata, db: AsyncSession
    ) -> None:
        import uuid

        from sqlalchemy import delete

        from app.models.metadata_cache import MetadataCache

        source_key = _cache_source_key(data_source_type)
        title = raw_title.strip()
        # Upsert: clear any existing row for this (title, source) so a
        # force_refresh re-run replaces the stale result instead of violating
        # the unique constraint, and different sources coexist as separate rows.
        await db.execute(
            delete(MetadataCache).where(
                MetadataCache.title == title,
                MetadataCache.source == source_key,
            )
        )
        cache_entry = MetadataCache(
            id=str(uuid.uuid4()),
            title=title,
            source=source_key,
            content_type=meta.content_type,
            metadata_json={
                "clean_title": meta.clean_title,
                "content_type": meta.content_type,
                "found": meta.found,
                "inferred_episode": meta.episode,
                "inferred_season": meta.season,
                "is_batch": meta.is_batch,
                "inferred_episode_start": meta.episode_start,
                "inferred_episode_end": meta.episode_end,
                "title_cn": meta.title_cn,
                "title_en": meta.title_en,
                "subtitle_group": meta.subtitle_group,
                "resolution": meta.resolution,
                "source": meta.source,
                "video_codec": meta.video_codec,
                "audio_codec": meta.audio_codec,
                "subtitle_type": meta.subtitle_type,
                "subtitle_langs": meta.subtitle_langs,
                "container": meta.container,
                "matched_entity": meta.matched_entity,
                "confidence": meta.confidence,
                "reason": meta.reason,
                "search_method": meta.search_method,
                "data_sources_used": meta.data_sources_used,
                "source_errors": meta.source_errors,
                "search_error": meta.search_error,
            },
        )
        db.add(cache_entry)
        await db.flush()


# Module-level lazy singleton
_agent_instance: UnifiedMetadataAgent | None = None


def get_agent() -> UnifiedMetadataAgent:
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = UnifiedMetadataAgent()
    return _agent_instance
