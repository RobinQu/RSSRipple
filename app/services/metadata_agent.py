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
import re
from typing import Any, ClassVar

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent  # noqa: F401 — kept for compat; deprecation warning is harmless
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import metadata_audio_resolver as _resolver
from app.services import metadata_repository as _repo
from app.services.metadata_audio import _detect_audio_work_type, _is_non_media
from app.services.metadata_episode_reconcile import _seasons_map_from, reconcile_episode
from app.services.metadata_failure import _classify_failure, _record_metadata_attempt
from app.services.metadata_prompts import _JUDGE_SYSTEM_PROMPT, _SYSTEM_PROMPT
from app.services.metadata_repository import _cache_source_key
from app.services.metadata_resource_meta import ResourceMetadata
from app.services.metadata_source_io import (
    _execute_get_tmdb_details,
    _execute_read_jina_url,
    _execute_search_exa_agent,
    _execute_search_jina,
    _execute_search_tmdb,
)
from app.services.metadata_sources import (
    DEFAULT_METADATA_SOURCE,
    SUPPORTED_METADATA_SOURCES,
    get_metadata_source_catalog,
    is_metadata_source_available,
    normalize_metadata_source_type,
    resolve_metadata_source,
)
from app.services.metadata_title_index import WorkTitleIndex, _normalize_title
from app.services.metadata_wiki_classify import (
    _classify_wikipedia_page,
    _infer_content_type_from_categories,
    _validate_matched_entity_kind,
)
from app.services.metadata_wiki_query import _candidate_queries, _clean_query, _work_name_prefix
from app.services.metadata_wikipedia_client import (
    _WIKIPEDIA_USER_AGENT,
    _execute_get_wikipedia_page,
    _execute_search_wikipedia,
    _fetch_wikipedia_page_image,
    _is_disambiguation_category,
    _wikipedia_client,
)
from app.services.runtime_config import runtime_config
from app.utils.time import utcnow

# Names re-exported from extracted leaf modules (Phase 0). Listing them here
# keeps ruff F401 from pruning symbols that metadata_agent no longer uses
# locally but still exposes to legacy callers (`from app.services.metadata_agent
# import SUPPORTED_METADATA_SOURCES`, etc.).
__all__ = [
    "DEFAULT_METADATA_SOURCE",
    "SUPPORTED_METADATA_SOURCES",
    "get_metadata_source_catalog",
    "is_metadata_source_available",
    "normalize_metadata_source_type",
    "resolve_metadata_source",
    # episode_reconcile: used by metadata_repository; re-exported for tests.
    "_seasons_map_from",
    "reconcile_episode",
    # wiki_query: _candidate_queries is used locally below; these two are not.
    "_clean_query",
    "_work_name_prefix",
    # wikipedia_client: the two _execute_* are used locally by the judge path
    # and audio resolver; these four are re-exported only for tests (ma.X).
    "_WIKIPEDIA_USER_AGENT",
    "_fetch_wikipedia_page_image",
    "_is_disambiguation_category",
    "_wikipedia_client",
    "_cache_source_key",
    "_normalize_title",
]

logger = logging.getLogger(__name__)


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
# S3: search-first + single-LLM-judge path (wikipedia)
#
# ReAct spends ~4-5 LLM calls per resource (think -> search -> think -> ... ->
# finalize). This path pre-computes the candidate wikipedia queries, runs them
# in parallel (no LLM), then makes ONE LLM call to judge the evidence and emit
# the finalize JSON. _run_react remains the fallback for other sources and for
# any judge call that fails or yields unparseable JSON.
# ---------------------------------------------------------------------------


def _parse_finalize_json(text: str) -> dict | None:
    """Extract the finalize JSON object from an LLM text response."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    candidate = fence.group(1) if fence else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(candidate[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


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

    # Cap the ReAct loop well below the default. A focused run needs ~3-5
    # tool calls (search + 1-2 page/details + finalize); 25 steps allows that
    # with headroom while preventing the runaway case where the agent chases
    # irrelevant pages for 20+ calls until it hits the limit. Hitting the cap
    # raises "Recursion limit..." which _classify_failure treats as transient
    # (retried with backoff), so legitimate-but-long runs get another shot.
    MAX_LANGGRAPH_RECURSION_LIMIT: ClassVar[int] = 25

    def __init__(self) -> None:
        self._model = ChatOpenAI(
            model=runtime_config.llm_model,
            api_key=runtime_config.llm_api_key,
            base_url=runtime_config.llm_base_url,
            temperature=0.1,
            # The upstream relay (LLM_BASE_URL) fails two ways, tuned
            # separately because they want opposite handling:
            #   - TLS drops (APIConnectionError): fast-fail, so retries are
            #     cheap. max_retries=3 (4 attempts, SDK exponential backoff)
            #     absorbs the bulk in-process instead of deferring each dropped
            #     resource to the next fetch cycle.
            #   - Slow-but-successful calls (APITimeoutError): a ReAct step
            #     with full prompt + history can exceed 30s. The SDK retries
            #     timeouts too, so such a call is killed 4x under timeout=30
            #     and never completes; timeout=60 lets that tail succeed on
            #     the first attempt. Worst case (a truly hung call) is
            #     ~4x60s + backoff ≈ 4 min per resource, acceptable for a
            #     background fetch.
            # A total failure is still classified transient (not cached) by
            # _classify_failure, so this only tightens the in-process budget.
            timeout=60,
            max_retries=3,
        )
        self._agents: dict[str, Any] = {}
        # Work-level short-circuit index (S1) - encapsulated in WorkTitleIndex.
        self._title_index_store = WorkTitleIndex()

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

    # ── Work-level short-circuit (S1) ──

    async def _find_known_work(
        self, resource: Any, db: AsyncSession
    ) -> tuple[str, str] | None:
        """Return (work_type, work_id) if the resource's pre-parsed title
        exactly (after normalization) matches one known TVSeries/Movie, else
        None. Ambiguous titles (mapping to >1 work) return None so the agent
        runs instead of guessing.
        """
        return await self._title_index_store.find(resource, db)

    # ── AudioWork resolution (ASMR / music / drama CD / radio) ──

    async def _resolve_audio_work(
        self,
        resource: Any,
        channel: Any,
        db: AsyncSession,
        audio_type: str,
        force_refresh: bool,  # noqa: ARG002 - kept for signature parity
    ) -> ResourceMetadata | None:
        """Resolve an audio-marked resource into an AudioWork entity."""
        return await _resolver._resolve_audio_work(
            resource, channel, db, audio_type, force_refresh
        )

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

        # 0b. Work-level short-circuit (S1): if the pre-parser's title already
        # matches a known TVSeries/Movie (exact after normalization), link
        # directly without an LLM call. New episodes/releases of an
        # already-identified work skip the agent entirely. Ambiguous titles
        # fall through to the agent. Not cached in MetadataCache (the cache is
        # keyed by raw_title; this index IS the cross-title cache) - the
        # resource is marked matched so the backfill won't revisit it.
        #
        # Fires even on force_refresh: force_refresh bypasses the *cache* (a
        # possibly-stale result), but the title index is live/current, so a
        # matching resource is linked correctly without an LLM call. This lets
        # the backfill (which uses force_refresh) short-circuit resources that
        # now match a known work (e.g. after a title cleanup) instead of
        # re-running the full agent for each.
        known = await self._find_known_work(resource, db)
        if known is not None:
            work_type, work_id = known
            if work_type == "movie":
                resource.movie_id = work_id
                resource.series_id = None
            else:
                resource.series_id = work_id
                resource.movie_id = None
            resource.metadata_matched_at = utcnow()
            resource.metadata_attempts = int(
                getattr(resource, "metadata_attempts", 0) or 0
            ) + 1
            resource.last_metadata_attempt_at = utcnow()
            resource.metadata_failure_type = None  # success
            if not resource.search_title:
                resource.search_title = (
                    resource.title_cn or resource.title_en or raw_title
                )[:200]
            logger.info(
                "[metadata_agent] short-circuit matched %r -> %s %s (no LLM)",
                raw_title[:80], work_type, work_id,
            )
            return ResourceMetadata(
                clean_title=resource.search_title or "",
                found=True,
                content_type=work_type,
            )

        # 0c. Non-media (software / cracked tools) -> non_work, never retried.
        if _is_non_media(raw_title):
            meta = ResourceMetadata(
                clean_title=raw_title[:200],
                found=False,
                content_type="tv",
                reason="non-media release (software / tool), not a TV/movie work",
            )
            await self._apply_to_resource(meta, resource, channel, db)
            _record_metadata_attempt(resource, meta)
            if _classify_failure(meta) != "transient":
                await self._set_cache(raw_title, data_source_type, meta, db)
            return meta

        # 0d. AudioWork detection: a title with strong audio-only markers
        # (ASMR / FLAC / soundtrack / drama CD / radio) is not a TV/movie work.
        # Resolve it into an AudioWork entity via a general-purpose source
        # (Wikipedia / Exa; TMDB falls back to Wikipedia) with a title-stub
        # fallback, so these resources stop cycling as non_work/not_found.
        # Runs AFTER the TV/movie short-circuit so an OP/ED theme whose title
        # already matches a known series still links to that series.
        audio_type = _detect_audio_work_type(raw_title)
        if audio_type is not None:
            meta = await self._resolve_audio_work(
                resource, channel, db, audio_type, force_refresh
            )
            if meta is not None:
                _record_metadata_attempt(resource, meta)
                if _classify_failure(meta) != "transient":
                    await self._set_cache(raw_title, data_source_type, meta, db)
                return meta

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

        # 2. Run metadata: search-first + single-LLM-judge for wikipedia (S3,
        # 1 LLM call + parallel searches); ReAct for other sources.
        if normalize_metadata_source_type(data_source_type) == "wikipedia":
            finalize_dict, search_info = await self._run_search_then_judge(
                raw_title, data_source_type, resource=resource
            )
        else:
            finalize_dict, search_info = await self._run_react(message, data_source_type)
        finalize_dict["search_method"] = search_info.get("method")
        finalize_dict["data_sources_used"] = search_info.get("data_sources_used") or []
        finalize_dict["source_errors"] = search_info.get("source_errors") or {}
        finalize_dict["search_error"] = search_info.get("error")

        # 3. Parse
        meta = ResourceMetadata.from_dict(finalize_dict)

        # B3: defense-in-depth - decline a Wikipedia match whose page is a
        # non-work entity (station/company/person/disambiguation). B1 (auto-link
        # gate) and B2 (judge prompt) already steer away from these, but a judge
        # slip or thin-categories fallthrough could still surface one; never
        # upsert a bogus TVSeries from a non-work page. Strips the carried
        # `categories` (a B3-only transport key) from matched_entity afterwards
        # so it never reaches the upsert/cache.
        meta = _validate_matched_entity_kind(meta)
        if meta.matched_entity:
            meta.matched_entity.pop("categories", None)

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

        if not runtime_config.llm_api_key:
            return ResourceMetadata(
                clean_title=raw_title.strip()[:100],
                found=False,
                reason="LLM API key not configured",
            )

        source = normalize_metadata_source_type(data_source_type)
        logger.info("[metadata_agent] process_title_only source=%s title=%r", source, raw_title[:200])
        message = self._build_title_only_message(raw_title, source)
        # S3: search-first + single-LLM-judge for wikipedia; ReAct otherwise.
        if source == "wikipedia":
            finalize_dict, search_info = await self._run_search_then_judge(raw_title, source)
        else:
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

    async def _run_search_then_judge(
        self,
        raw_title: str,
        data_source_type: str | None = None,
        resource: Any | None = None,
    ) -> tuple[dict, dict]:
        """Search-first + single-LLM-judge path (S3) for the wikipedia source.

        Runs candidate wikipedia searches in parallel (no LLM), then ONE LLM
        call to judge the evidence and emit the finalize JSON - cutting the
        ~4-5 LLM calls of ReAct to 1. Falls back to ``_run_react`` when there
        is no usable query, the judge call fails, or its JSON is unparseable.
        """
        source = normalize_metadata_source_type(data_source_type)
        queries = _candidate_queries(raw_title, resource)
        if not queries:
            return await self._run_react(
                self._build_title_only_message(raw_title, source), source
            )

        raw_results = await asyncio.gather(
            *(_execute_search_wikipedia(q, lang) for (q, lang) in queries),
            return_exceptions=True,
        )
        source_errors: dict[str, str] = {}
        # Collect top candidates (dedup by page_id) across variants.
        seen_pids: set = set()
        top: list[dict] = []
        for (q, lang), res in zip(queries, raw_results):
            if isinstance(res, Exception):
                source_errors[f"wikipedia:{lang}"] = f"{type(res).__name__}: {res}"[:200]
                continue
            if not isinstance(res, dict) or not res.get("success"):
                source_errors[f"wikipedia:{lang}"] = (
                    res.get("error", "search failed")[:200]
                    if isinstance(res, dict)
                    else "no result"
                )
                continue
            for cand in res.get("data", [])[:3]:
                pid = cand.get("page_id")
                if pid and pid in seen_pids:
                    continue
                if pid:
                    seen_pids.add(pid)
                top.append({"query": q, "lang": lang, **cand})
                if len(top) >= 6:
                    break
            if len(top) >= 6:
                break

        # Fetch full pages in parallel - categories are the strongest TV-vs-movie
        # signal and the search summary alone is often too thin for the judge to
        # confirm a match (this is what made ReAct's get_wikipedia_page step
        # worth its extra turn).
        page_results = await asyncio.gather(
            *(_execute_get_wikipedia_page(c["title"], c["lang"]) for c in top),
            return_exceptions=True,
        )
        evidence: list[dict] = []
        for cand, pres in zip(top, page_results):
            entry = dict(cand)
            if isinstance(pres, dict) and pres.get("data"):
                d = pres["data"]
                if d.get("disambiguation"):
                    entry["disambiguation"] = True
                entry["categories"] = list(d.get("categories", [])[:10])
                if d.get("summary"):
                    entry["summary"] = d["summary"][:400]
                if not entry.get("url") and d.get("url"):
                    entry["url"] = d.get("url")
                if d.get("poster_url"):
                    entry["poster_url"] = d["poster_url"]
            elif isinstance(pres, Exception):
                source_errors[f"page:{cand.get('lang')}"] = f"{type(pres).__name__}: {pres}"[:200]
            evidence.append(entry)

        # Deterministic auto-link: when a search result's title clearly matches
        # its query (similarity >= AUTO_LINK_THRESHOLD after OpenCC trad/simp
        # normalization), trust it without the LLM judge. The mini-LLM judge
        # often rejects obvious trad<->simp matches - e.g. simplified
        # "说出这边...传说" vs Wikipedia's traditional "說出這邊...傳說。" -
        # even though Wikipedia returned it as the top result.
        from app.services.metadata_service import AUTO_LINK_THRESHOLD
        from app.services.text_normalizer import similarity_score

        # Match each candidate's title against ALL candidate queries (not just
        # the one that first surfaced it). Page-id dedup above may associate a
        # page with a noisier long query even though a cleaner prefix query
        # also returned it - taking the max picks the clean match.
        all_query_strs = [q for q, _ in queries if q]
        best_auto: dict | None = None
        best_auto_score = 0
        best_auto_query = ""
        for e in evidence:
            if e.get("disambiguation"):
                continue
            title = e.get("title") or ""
            if not title or not all_query_strs:
                continue
            q = max(all_query_strs, key=lambda qq: similarity_score(qq, title))
            score = similarity_score(q, title)
            if score > best_auto_score:
                best_auto_score = score
                best_auto = e
                best_auto_query = q
        if best_auto is not None and best_auto_score >= AUTO_LINK_THRESHOLD:
            cats = best_auto.get("categories") or []
            page_kind = _classify_wikipedia_page(cats, best_auto.get("summary") or "")
            # B1: only auto-link a genuine creative work. A station / platform /
            # company / person / disambiguation page can title-match the query
            # (ViuTV, TVB, ...) well above the threshold - linking it on title
            # similarity alone is how the ViuTV television-station page became a
            # bogus series. non_work / ambiguous fall through to the LLM judge,
            # which can pick a different candidate or confirm no match.
            if page_kind == "work":
                ct = _infer_content_type_from_categories(cats)
                page_id = best_auto.get("page_id")
                lang = best_auto.get("lang")
                wiki_title = best_auto.get("title")
                finalize_dict = {
                    "found": True,
                    "clean_title": best_auto_query or wiki_title,
                    "content_type": ct,
                    "title_cn": wiki_title if lang == "zh" else None,
                    "title_en": wiki_title if lang == "en" else None,
                    "matched_entity": {
                        "external_id": f"wikipedia:{page_id}" if page_id else None,
                        "external_source": "wikipedia",
                        "title_cn": wiki_title if lang == "zh" else None,
                        "title_en": wiki_title if lang == "en" else None,
                        "description": (best_auto.get("summary") or "")[:500] or None,
                        "poster_url": best_auto.get("poster_url"),
                        "wikipedia_url": best_auto.get("url"),
                        "categories": list(cats[:10]),  # B3: carried for validation
                    },
                    "confidence": 0.9,
                    "reason": f"auto-linked wikipedia result (title similarity {best_auto_score})",
                }
                logger.info(
                    "[metadata_agent] auto-link %r -> %r (sim=%d, work, no judge)",
                    raw_title[:80], wiki_title, best_auto_score,
                )
                return finalize_dict, {
                    "method": "search_then_autolink",
                    "data_sources_used": ["wikipedia"],
                    "source_errors": source_errors,
                    "error": None,
                }
            logger.info(
                "[metadata_agent] auto-link skipped for %r: top result %r is %s "
                "(sim=%d); deferring to judge",
                raw_title[:80], best_auto.get("title"), page_kind, best_auto_score,
            )
            # non_work / ambiguous -> fall through to the LLM judge below

        evidence_text = (
            "\n".join(
                f"[{i}] title={e.get('title')} page_id={e.get('page_id')} "
                f"url={e.get('url')} lang={e.get('lang')}\n    "
                f"categories={e.get('categories', [])[:6]}\n    "
                f"poster_url={e.get('poster_url')}\n    "
                f"summary={e.get('summary', '')[:280]}"
                for i, e in enumerate(evidence[:6], 1)
            )
            or "(no wikipedia results found for any variant)"
        )
        hints = ""
        if resource is not None:
            hints = (
                f"Pre-parsed hints: title_cn={getattr(resource, 'title_cn', None)!r} "
                f"title_en={getattr(resource, 'title_en', None)!r} "
                f"episode={getattr(resource, 'episode', None)} "
                f"season={getattr(resource, 'season', None)}"
            )

        user_msg = (
            f"RSS title: {raw_title}\n{hints}\n\n"
            f"Wikipedia evidence:\n{evidence_text}\n\n"
            f"Return the finalize JSON now."
        )
        try:
            from langchain_core.messages import HumanMessage, SystemMessage

            resp = await self._model.ainvoke(
                [SystemMessage(content=_JUDGE_SYSTEM_PROMPT), HumanMessage(content=user_msg)]
            )
        except Exception as e:
            logger.warning(
                "[metadata_agent] judge call failed for %r: %s; falling back to ReAct",
                raw_title[:80], e,
            )
            return await self._run_react(
                self._build_title_only_message(raw_title, source), source
            )
        content = getattr(resp, "content", "") or ""
        if isinstance(content, list):  # some models return structured content
            content = "".join(getattr(c, "text", str(c)) for c in content)
        finalize_dict = _parse_finalize_json(content)
        if finalize_dict is None:
            logger.warning(
                "[metadata_agent] judge returned unparseable JSON for %r; falling back to ReAct",
                raw_title[:80],
            )
            return await self._run_react(
                self._build_title_only_message(raw_title, source), source
            )
        # The single-call judge (especially on a mini model) can be conservative
        # and return found=False despite relevant evidence existing - a false
        # negative ReAct's multi-turn reasoning would catch. When that happens,
        # spend the extra ReAct run to verify; clear not-founds (no evidence at
        # all) are accepted as-is. Found=True results keep the fast 1-call path.
        if not finalize_dict.get("found") and evidence:
            logger.info(
                "[metadata_agent] judge found=False with %d candidates for %r; ReAct second opinion",
                len(evidence), raw_title[:80],
            )
            return await self._run_react(
                self._build_title_only_message(raw_title, source), source
            )
        # B3: carry the matched page's categories onto matched_entity so
        # process() can defense-check the entity kind. The judge returns
        # external_id "wikipedia:<page_id>"; find the evidence page with that
        # page_id and copy its categories (plus a description if missing).
        if finalize_dict.get("found"):
            me = finalize_dict.get("matched_entity") or {}
            ext_id = me.get("external_id") or ""
            pid = ext_id.split(":", 1)[1] if ext_id.startswith("wikipedia:") else None
            if pid:
                for e in evidence:
                    if str(e.get("page_id")) == str(pid):
                        me["categories"] = list(e.get("categories", [])[:10])
                        if not me.get("description"):
                            me["description"] = (e.get("summary") or "")[:500] or None
                        break
                finalize_dict["matched_entity"] = me
        finalize_dict.setdefault("clean_title", "")
        finalize_dict.setdefault("content_type", "tv")
        logger.info(
            "[metadata_agent] judge done %r found=%s",
            raw_title[:80], finalize_dict.get("found"),
        )
        return finalize_dict, {
            "method": "search_then_judge",
            "data_sources_used": ["wikipedia"],
            "source_errors": source_errors,
            "error": None,
        }

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
                                err = content.get("error", "no results")
                                source_errors.setdefault("wikipedia", err)
                                # Surface infra failures ("Wikipedia request
                                # failed: ...") on search_error so
                                # _classify_failure treats them as transient
                                # and they are retried, not cached as a
                                # permanent not_found. A PageError ("Page not
                                # found") carries no transient marker, so it
                                # still classifies as not_found.
                                search_error = search_error or f"Wikipedia: {err}"
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
        return await _repo._apply_to_resource(meta, resource, channel, db)

    # ── Cache ──

    async def _get_cache(
        self, raw_title: str, data_source_type: str | None, db: AsyncSession
    ) -> ResourceMetadata | None:
        return await _repo._get_cache(raw_title, data_source_type, db)

    async def _set_cache(
        self, raw_title: str, data_source_type: str | None, meta: ResourceMetadata, db: AsyncSession
    ) -> None:
        return await _repo._set_cache(raw_title, data_source_type, meta, db)


# Module-level lazy singleton
_agent_instance: UnifiedMetadataAgent | None = None


def get_agent() -> UnifiedMetadataAgent:
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = UnifiedMetadataAgent()
    return _agent_instance


def reset_metadata_agent() -> None:
    """Drop the cached agent so the next :func:`get_agent` call rebuilds it.

    Call after LLM config (model / api key / base url) changes via the system
    settings UI so the new values take effect without an app restart.
    """
    global _agent_instance
    _agent_instance = None
