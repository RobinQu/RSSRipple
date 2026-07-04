"""Metadata matching service for TVSeries and Movie entities.

Matching flow for a FileResource (per AGENTS.md "Metadata 匹配流程"):
1. Already linked (movie_id / series_id set) → return.
2. ChannelRawTitleMapping exact match by (channel_id, raw_title).
3. Local DB match: exact (title_cn/title_en) then fuzzy (ratio >= 70; auto-link at >=85).
4. Unified MetadataAgent (ReAct agent) — uses one selected metadata source
   (channel.metadata_agent_enabled == True).
5. Link FileResource.movie_id or FileResource.series_id.

Poster caching: poster URLs returned by LLM are downloaded to POSTER_CACHE_DIR
using a sha256-based filename, and the DB pointer is updated to /posters/<file>.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime, UTC
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.utils.time import utcnow
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.movie import Movie
from app.models.series import TVSeries
from app.services.text_normalizer import normalize_title, similarity_score
from app.services import fts as fts_service

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 70
AUTO_LINK_THRESHOLD = 85


# ---------------------------------------------------------------------------
# external_id canonicalization
#
# Exa Agent Search returns TMDB ids in inconsistent shapes:
#   "TMDB:82684", "TMDB 82684", "TMDB TV 82684 / season 4", "82684"
# All of them refer to the same TMDB work, but our naive `external_id`
# lookup would treat each shape as a separate row and keep spawning
# duplicate TVSeries/Movie entities on every fetch.
# The canonicalizer collapses those shapes into a single canonical form
# (e.g. ``tmdb:82684``) so upserts converge.
# ---------------------------------------------------------------------------

_TMDB_DIGITS_RE = re.compile(r"tmdb[^0-9]*(\d{2,10})", re.IGNORECASE)
_LEADING_DIGITS_RE = re.compile(r"^\s*(\d{2,10})\s*$")


def canonicalize_external_id(
    raw_id: str | None,
    source: str | None,
    content_type: str | None = None,
) -> str | None:
    """Return a stable canonical form of ``raw_id`` for upsert matching.

    Rules:
      * Any string containing ``tmdb`` and digits → ``tmdb:{digits}``.
      * ``source == "tmdb"`` combined with a pure-digit id → ``tmdb:{digits}``.
      * IMDb ids (``tt`` + digits) → ``imdb:{tt…}``.
      * Otherwise: lowercase + collapse whitespace, and drop known clutter
        such as ``/ season N`` tails.

    Never fabricates an id; returns None only when the input is falsy.
    """
    if raw_id is None:
        return None
    s = str(raw_id).strip()
    if not s:
        return None

    # Strip trailing "/ season N" or similar decoration.
    s_clean = re.sub(r"[\s/,;|]+season[\s#:_-]*\d+\s*$", "", s, flags=re.IGNORECASE)
    s_clean = re.sub(r"\s+", " ", s_clean).strip()

    lower = s_clean.lower()
    # TMDB detection — any "tmdb" prefix or when source declares tmdb.
    if "tmdb" in lower:
        m = _TMDB_DIGITS_RE.search(lower)
        if m:
            return f"tmdb:{m.group(1)}"
    if (source or "").strip().lower() == "tmdb":
        m = _LEADING_DIGITS_RE.match(lower)
        if m:
            return f"tmdb:{m.group(1)}"
        m = re.search(r"(\d{2,10})", lower)
        if m:
            return f"tmdb:{m.group(1)}"

    # IMDb ids
    m = re.match(r"^(tt\d{5,})$", lower)
    if m:
        return f"imdb:{m.group(1)}"

    return lower or None


# ---------------------------------------------------------------------------
# Title extraction helpers
# ---------------------------------------------------------------------------

_LEADING_BRACKET_RE = re.compile(r"^\[[^\]]*\]\s*")
_EPISODE_TAIL_RE = re.compile(r"\s*-\s*\d+\b.*$")
_SEASON_EPISODE_RE = re.compile(r"\s+S\d+E\d+\b.*$", re.IGNORECASE)
_TRAILING_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")


def extract_search_title(resource: Any) -> str:
    """Extract a base searchable title from a FileResource (sync, no LLM).

    Priority:
    1. ``title_cn`` or ``title_en`` (already parsed by field_mapping)
    2. ``parse_title(title_raw)`` — strips subtitle group, episode, quality
    3. Simple regex cleanup of ``title_raw`` as a last resort
    """
    title = resource.title_cn or resource.title_en
    if title and title.strip():
        return title.strip()

    raw = getattr(resource, "title_raw", None) or ""
    if not raw.strip():
        return raw

    cleaned = _LEADING_BRACKET_RE.sub("", raw)
    cleaned = _EPISODE_TAIL_RE.sub("", cleaned)
    cleaned = _SEASON_EPISODE_RE.sub("", cleaned)
    cleaned = _TRAILING_BRACKET_RE.sub("", cleaned)
    cleaned = cleaned.strip()
    return cleaned or raw.strip()


# ---------------------------------------------------------------------------
# Poster caching
# ---------------------------------------------------------------------------

async def download_and_cache_poster(remote_url: str | None) -> str | None:
    """Download a poster image to the local cache directory.

    Filename is ``{sha256(url)[:16]}.{ext}``. Returns the local URL path
    ``/posters/<filename>`` on success, or None on failure.
    Skips URLs that are already local (``/posters/...``).
    """
    if not remote_url:
        return None
    if remote_url.startswith("/posters/"):
        return remote_url
    if not (remote_url.startswith("http://") or remote_url.startswith("https://")):
        return None
    if not settings.poster_cache_dir:
        return None

    cache_dir = Path(settings.poster_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    parsed = urlparse(remote_url)
    ext = (Path(parsed.path).suffix or ".jpg").lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    ext = ext.lstrip(".")

    digest = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()[:16]
    filename = f"{digest}.{ext}"
    local_path = cache_dir / filename

    if local_path.exists():
        return f"/posters/{filename}"

    def _download() -> bytes | None:
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                resp = client.get(remote_url)
                resp.raise_for_status()
                return resp.content
        except Exception as e:
            logger.warning("[poster] download failed %s: %s", remote_url[:80], e)
            return None

    content = await asyncio.to_thread(_download)
    if not content:
        return None
    try:
        local_path.write_bytes(content)
        return f"/posters/{filename}"
    except Exception as e:
        logger.warning("[poster] write failed %s: %s", filename, e)
        return None


# ---------------------------------------------------------------------------
# Local DB matching
# ---------------------------------------------------------------------------

async def match_series_by_title(db: AsyncSession, title: str) -> tuple[TVSeries | None, int]:
    """Find best matching TVSeries in local DB. Returns (entity, score 0-100).

    Uses FTS5 trigram search for candidate retrieval (no full-table scan),
    then computes bigram Dice similarity for precise ranking.
    """
    if not title:
        return None, 0
    norm = normalize_title(title)
    if not norm:
        return None, 0

    # 1. Exact match on original title_cn/title_en (fast SQL index lookup)
    result = await db.execute(
        select(TVSeries).where(
            or_(
                TVSeries.title_cn == title,
                TVSeries.title_en == title,
            )
        )
    )
    series = result.scalars().first()
    if series:
        return series, 100

    # 2. FTS5 candidate retrieval + similarity scoring
    candidate_ids = await fts_service.search_series_fts(db, title, limit=30)
    if candidate_ids:
        result = await db.execute(select(TVSeries).where(TVSeries.id.in_(candidate_ids)))
        candidates = result.scalars().all()
    else:
        # FTS index may be empty/out of sync — fall back to full-table scan
        result = await db.execute(select(TVSeries))
        candidates = result.scalars().all()

    best: TVSeries | None = None
    best_score = 0
    for s in candidates:
        titles = [s.title_cn, s.title_en, *(s.aliases or [])]
        score = max((similarity_score(norm, t) for t in titles if t), default=0)
        if score > best_score:
            best_score = score
            best = s

    if best_score >= FUZZY_THRESHOLD:
        return best, best_score
    return None, 0


async def match_movie_by_title(db: AsyncSession, title: str) -> tuple[Movie | None, int]:
    """Find best matching Movie in local DB. Returns (entity, score 0-100).

    Uses FTS5 trigram search for candidate retrieval, then bigram Dice
    similarity for precise ranking.
    """
    if not title:
        return None, 0
    norm = normalize_title(title)
    if not norm:
        return None, 0

    # 1. Exact match
    result = await db.execute(
        select(Movie).where(
            or_(
                Movie.title_cn == title,
                Movie.title_en == title,
            )
        )
    )
    movie = result.scalars().first()
    if movie:
        return movie, 100

    # 2. FTS5 candidate retrieval + similarity scoring
    candidate_ids = await fts_service.search_movie_fts(db, title, limit=30)
    if candidate_ids:
        result = await db.execute(select(Movie).where(Movie.id.in_(candidate_ids)))
        candidates = result.scalars().all()
    else:
        # FTS index may be empty/out of sync — fall back to full-table scan
        result = await db.execute(select(Movie))
        candidates = result.scalars().all()

    best: Movie | None = None
    best_score = 0
    for m in candidates:
        titles = [m.title_cn, m.title_en, *(m.aliases or [])]
        score = max((similarity_score(norm, t) for t in titles if t), default=0)
        if score > best_score:
            best_score = score
            best = m

    if best_score >= FUZZY_THRESHOLD:
        return best, best_score
    return None, 0


# ---------------------------------------------------------------------------
# Metadata search (delegates to UnifiedMetadataAgent)
# ---------------------------------------------------------------------------


async def search_metadata_via_llm(
    title: str,
    data_source_type: str | None = None,
) -> list[dict]:
    """Search for metadata using the unified metadata agent.

    Delegates to ``UnifiedMetadataAgent.process_title_only()`` for title cleaning
    and metadata search via one selected source.
    Returns a list of candidate dicts (same shape as before) so callers work unchanged.
    """
    from app.services.metadata_agent import get_agent

    try:
        logger.info(
            "[metadata] agent search start title=%r data_source_type=%s",
            title[:160], data_source_type,
        )
        result = await get_agent().process_title_only(title, data_source_type)
    except Exception as e:
        logger.warning("[metadata] Agent search failed for %r: %s", title[:60], e)
        return []

    if not result.found:
        if result.ambiguous and result.ambiguous_candidates:
            return result.ambiguous_candidates
        return []

    candidates: list[dict] = []
    if result.matched_entity:
        candidates.append(result.matched_entity)
    if result.ambiguous and result.ambiguous_candidates:
        candidates.extend(result.ambiguous_candidates)

    logger.info(
        "[metadata] agent search done title=%r data_source_type=%s found=%s candidates=%d error=%s",
        title[:160],
        data_source_type,
        result.found,
        len(candidates),
        result.search_error,
    )
    return candidates


# ---------------------------------------------------------------------------
# Entity upsert helpers
# ---------------------------------------------------------------------------

def _parse_date(val: Any) -> date | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()
    # Try full date/time formats first so a YYYY-MM-DD string isn't interpreted as YYYY.
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            # %Y-%m-%d consumes 10 characters (4+2+2 + 2 dashes) even though the
            # format string itself is 8 characters. Use the full input slice.
            if len(s) >= len(fmt):
                candidate = s
                # If the string is longer than needed, take only a prefix of the
                # appropriate length for the format.
                if fmt == "%Y-%m-%d":
                    candidate = s[:10]
                else:
                    candidate = s[:19]
                return datetime.strptime(candidate, fmt).date()
        except (ValueError, TypeError):
            continue
    # Year-only: 4-digit string that isn't a longer date
    try:
        if len(s) == 4 and s.isdigit():
            return date(int(s), 1, 1)
    except (ValueError, TypeError):
        pass
    return None


async def create_or_update_series_from_external(db: AsyncSession, data: dict) -> TVSeries:
    """Upsert a TVSeries by canonicalized external_id, then by exact title fallback.

    External sources (especially Exa Agent) can return the same TMDB id in
    inconsistent shapes on subsequent fetches; naive equality on ``external_id``
    would then keep inserting duplicate rows. We normalize the id via
    :func:`canonicalize_external_id` for the primary lookup, and, if that
    still misses, fall back to an exact case-sensitive match on
    ``title_cn`` / ``title_en`` / ``original_title`` — the strong signal that
    a fresh Exa response describes an already-known work.
    """
    raw_external_id = data.get("external_id")
    raw_source = data.get("external_source")
    content_type = data.get("content_type")
    canonical_id = canonicalize_external_id(raw_external_id, raw_source, content_type)

    # Primary lookup — canonical id preferred, but keep matching legacy rows
    # written before canonicalization existed. ``llm_search`` is a legacy
    # source label kept for compatibility.
    lookup_ids = {i for i in (canonical_id, raw_external_id) if i}
    lookup_sources = {s for s in (raw_source, "llm_search") if s}

    series: TVSeries | None = None
    if lookup_ids:
        stmt = select(TVSeries).where(TVSeries.external_id.in_(lookup_ids))
        if lookup_sources:
            stmt = stmt.where(TVSeries.external_source.in_(lookup_sources))
        result = await db.execute(stmt)
        series = result.scalars().first()

    # Fallback: same work returned with a fresh external_id shape. Match by
    # any of the canonical title columns (case-sensitive; titles are already
    # normalized by upstream extraction).
    if series is None:
        title_candidates = [
            t for t in (
                data.get("title_cn"),
                data.get("title_en"),
                data.get("original_title"),
            ) if t
        ]
        if title_candidates:
            title_result = await db.execute(
                select(TVSeries).where(
                    or_(
                        TVSeries.title_cn.in_(title_candidates),
                        TVSeries.title_en.in_(title_candidates),
                        TVSeries.original_title.in_(title_candidates),
                    )
                )
            )
            series = title_result.scalars().first()

    if series:
        # Migrate legacy/inconsistent identifiers to the canonical form so the
        # next upsert converges even faster.
        if canonical_id:
            series.external_id = canonical_id
        if raw_source and raw_source != "llm_search":
            series.external_source = raw_source
        series.description = data.get("description") or series.description
        if data.get("rating") is not None:
            series.rating = data.get("rating")
        series.original_title = data.get("original_title") or series.original_title
        series.status = data.get("status") or series.status
        if data.get("number_of_episodes") is not None:
            series.number_of_episodes = data.get("number_of_episodes")
        if data.get("number_of_seasons") is not None:
            series.number_of_seasons = data.get("number_of_seasons")
        sd = _parse_date(data.get("start_date"))
        if sd:
            series.start_date = sd
        ed = _parse_date(data.get("end_date"))
        if ed:
            series.end_date = ed
        if data.get("genre"):
            series.genre = data.get("genre")
        if data.get("title_cn"):
            series.title_cn = series.title_cn or data.get("title_cn")
        if data.get("title_en"):
            series.title_en = series.title_en or data.get("title_en")

        existing_titles = {t for t in [series.title_cn, series.title_en, *(series.aliases or [])] if t}
        new_aliases = list(series.aliases or [])
        for t in (data.get("title_cn"), data.get("title_en"), data.get("original_title")):
            if t and t not in existing_titles and t not in new_aliases:
                new_aliases.append(t)
                existing_titles.add(t)
        series.aliases = new_aliases or None

        remote_poster = data.get("poster_url")
        if remote_poster and not (series.poster_url or "").startswith("/posters/"):
            local_url = await download_and_cache_poster(remote_poster)
            series.poster_url = local_url or remote_poster
        series.content_type = "tv"
        await fts_service.upsert_series_fts(db, series)
        return series

    # Create
    remote_poster = data.get("poster_url")
    local_url = await download_and_cache_poster(remote_poster)
    title_cn = data.get("title_cn")
    title_en = data.get("title_en") or data.get("original_title")
    aliases: list[str] = []
    for t in (title_cn, title_en, data.get("original_title")):
        if t and t not in aliases:
            aliases.append(t)
    series = TVSeries(
        title_cn=title_cn,
        title_en=title_en,
        original_title=data.get("original_title"),
        aliases=aliases or None,
        external_id=canonical_id or raw_external_id,
        external_source=data.get("external_source", "llm_search"),
        description=data.get("description"),
        poster_url=local_url or remote_poster,
        rating=data.get("rating"),
        genre=data.get("genre") or [],
        status=data.get("status"),
        number_of_episodes=data.get("number_of_episodes"),
        number_of_seasons=data.get("number_of_seasons"),
        start_date=_parse_date(data.get("start_date")),
        end_date=_parse_date(data.get("end_date")),
        content_type="tv",
    )
    db.add(series)
    await db.flush()
    await fts_service.upsert_series_fts(db, series)
    return series


async def create_or_update_movie_from_external(db: AsyncSession, data: dict) -> Movie:
    """Upsert a Movie by canonicalized external_id, then by exact title fallback.

    See :func:`create_or_update_series_from_external` for the rationale.
    """
    raw_external_id = data.get("external_id")
    raw_source = data.get("external_source")
    content_type = data.get("content_type")
    canonical_id = canonicalize_external_id(raw_external_id, raw_source, content_type)

    lookup_ids = {i for i in (canonical_id, raw_external_id) if i}
    lookup_sources = {s for s in (raw_source, "llm_search") if s}

    movie: Movie | None = None
    if lookup_ids:
        stmt = select(Movie).where(Movie.external_id.in_(lookup_ids))
        if lookup_sources:
            stmt = stmt.where(Movie.external_source.in_(lookup_sources))
        result = await db.execute(stmt)
        movie = result.scalars().first()

    if movie is None:
        title_candidates = [
            t for t in (
                data.get("title_cn"),
                data.get("title_en"),
                data.get("original_title"),
            ) if t
        ]
        if title_candidates:
            title_result = await db.execute(
                select(Movie).where(
                    or_(
                        Movie.title_cn.in_(title_candidates),
                        Movie.title_en.in_(title_candidates),
                        Movie.original_title.in_(title_candidates),
                    )
                )
            )
            movie = title_result.scalars().first()

    if movie:
        if canonical_id:
            movie.external_id = canonical_id
        if raw_source and raw_source != "llm_search":
            movie.external_source = raw_source
        movie.description = data.get("description") or movie.description
        if data.get("rating") is not None:
            movie.rating = data.get("rating")
        movie.original_title = data.get("original_title") or movie.original_title
        movie.status = data.get("status") or movie.status
        rd = _parse_date(data.get("release_date"))
        if rd:
            movie.release_date = rd
        if data.get("runtime") is not None:
            movie.runtime = data.get("runtime")
        if data.get("genre"):
            movie.genre = data.get("genre")
        if data.get("title_cn"):
            movie.title_cn = movie.title_cn or data.get("title_cn")
        if data.get("title_en"):
            movie.title_en = movie.title_en or data.get("title_en")

        existing_titles = {t for t in [movie.title_cn, movie.title_en, *(movie.aliases or [])] if t}
        new_aliases = list(movie.aliases or [])
        for t in (data.get("title_cn"), data.get("title_en"), data.get("original_title")):
            if t and t not in existing_titles and t not in new_aliases:
                new_aliases.append(t)
                existing_titles.add(t)
        movie.aliases = new_aliases or None

        remote_poster = data.get("poster_url")
        if remote_poster and not (movie.poster_url or "").startswith("/posters/"):
            local_url = await download_and_cache_poster(remote_poster)
            movie.poster_url = local_url or remote_poster
        movie.content_type = "movie"
        await fts_service.upsert_movie_fts(db, movie)
        return movie

    remote_poster = data.get("poster_url")
    local_url = await download_and_cache_poster(remote_poster)
    title_cn = data.get("title_cn")
    title_en = data.get("title_en") or data.get("original_title")
    aliases: list[str] = []
    for t in (title_cn, title_en, data.get("original_title")):
        if t and t not in aliases:
            aliases.append(t)
    movie = Movie(
        title_cn=title_cn,
        title_en=title_en,
        original_title=data.get("original_title"),
        aliases=aliases or None,
        external_id=canonical_id or raw_external_id,
        external_source=data.get("external_source", "llm_search"),
        description=data.get("description"),
        poster_url=local_url or remote_poster,
        rating=data.get("rating"),
        genre=data.get("genre") or [],
        status=data.get("status"),
        release_date=_parse_date(data.get("release_date")),
        runtime=data.get("runtime"),
        content_type="movie",
    )
    db.add(movie)
    await db.flush()
    await fts_service.upsert_movie_fts(db, movie)
    return movie


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

async def fetch_and_link_metadata(db: AsyncSession, resource: Any, channel: Any) -> None:
    """Match metadata for a newly-created FileResource and set its FKs.

    Implements the 4-layer matching strategy from AGENTS.md.
    """
    # Layer 1: already linked
    if resource.series_id or resource.movie_id:
        return

    # Layer 2: ChannelRawTitleMapping
    # Primary lookup: by normalized search_title_key (handles episode/resolution variations)
    search_key = normalize_title(extract_search_title(resource))
    mapping = None
    if search_key:
        mapping_result = await db.execute(
            select(ChannelRawTitleMapping).where(
                ChannelRawTitleMapping.channel_id == channel.id,
                ChannelRawTitleMapping.search_title_key == search_key,
            )
        )
        mapping = mapping_result.scalars().first()
    # Fallback: by exact raw_title (compatibility with pre-search_key mappings)
    if not mapping:
        mapping_result = await db.execute(
            select(ChannelRawTitleMapping).where(
                ChannelRawTitleMapping.channel_id == channel.id,
                ChannelRawTitleMapping.raw_title == resource.title_raw,
            )
        )
        mapping = mapping_result.scalars().first()
    if mapping:
        if mapping.series_id:
            resource.series_id = mapping.series_id
            resource.movie_id = None
        elif mapping.movie_id:
            resource.movie_id = mapping.movie_id
            resource.series_id = None
        if mapping.search_title_override:
            resource.search_title = mapping.search_title_override
        resource.metadata_matched_at = utcnow()
        return

    # Layer 3: local match
    search_title = resource.search_title or extract_search_title(resource)
    if not search_title:
        return

    series, s_ratio = await match_series_by_title(db, search_title)
    movie, m_ratio = await match_movie_by_title(db, search_title)

    # Auto-link only at >=85 ratio
    if series and s_ratio >= AUTO_LINK_THRESHOLD and (movie is None or s_ratio >= m_ratio):
        resource.series_id = series.id
        resource.metadata_matched_at = utcnow()
        if not series.poster_url or not (series.poster_url or "").startswith("/posters/"):
            pass  # poster already handled if set
        return
    if movie and m_ratio >= AUTO_LINK_THRESHOLD and (series is None or m_ratio > s_ratio):
        resource.movie_id = movie.id
        resource.metadata_matched_at = utcnow()
        return

    # NOTE: 70-84 matches are skipped (too ambiguous) and fall through to LLM layer.

    # Layer 4: selected-source metadata search
    if not channel.metadata_agent_enabled:
        return

    try:
        from app.services.metadata_agent import DEFAULT_METADATA_SOURCE

        results = await search_metadata_via_llm(search_title, DEFAULT_METADATA_SOURCE)
    except Exception as e:
        logger.warning("[metadata] LLM search failed for %r: %s", search_title[:60], e)
        return

    if not results:
        return
    best = results[0]
    try:
        if best.get("content_type") == "movie":
            movie_entity = await create_or_update_movie_from_external(db, best)
            resource.movie_id = movie_entity.id
            resource.series_id = None
        else:
            series_entity = await create_or_update_series_from_external(db, best)
            resource.series_id = series_entity.id
            resource.movie_id = None
        resource.metadata_matched_at = utcnow()
    except Exception as e:
        logger.warning("[metadata] Failed to link via LLM for %r: %s", search_title[:60], e)


async def manual_search_metadata(
    db: AsyncSession,
    search_title: str,
    content_type: str,
    data_source_type: str | None = None,
) -> list[dict]:
    """Search for metadata candidates. No persistence.

    When ``data_source_type == "local"``, searches the local TVSeries/Movie
    library via FTS5 instead of calling the LLM agent. This allows users to
    match resources against already-known works without external API calls.
    """
    logger.info(
        "[metadata] manual_search start title=%r content_type=%s data_source_type=%s",
        search_title[:160], content_type, data_source_type,
    )

    # Local library data source — search existing TVSeries/Movie via FTS5
    if data_source_type == "local":
        results = await _search_local_library(db, search_title, content_type)
        logger.info(
            "[metadata] manual_search (local) done title=%r candidates=%d",
            search_title[:160], len(results),
        )
        return results

    results = await search_metadata_via_llm(search_title, data_source_type)
    normalized: list[dict] = []
    for result in results:
        item = dict(result)
        if item.get("content_type") not in ("tv", "movie"):
            item["content_type"] = content_type if content_type in ("tv", "movie") else "tv"
        normalized.append(item)
    results = normalized
    if content_type in ("tv", "movie"):
        # Prefer content type but don't strictly filter — return all candidates
        preferred = [r for r in results if r.get("content_type") == content_type]
        if preferred:
            logger.info(
                "[metadata] manual_search done title=%r preferred_candidates=%d total_candidates=%d",
                search_title[:160], len(preferred), len(results),
            )
            return preferred
    logger.info(
        "[metadata] manual_search done title=%r candidates=%d",
        search_title[:160], len(results),
    )
    return results


async def _search_local_library(
    db: AsyncSession,
    search_title: str,
    content_type: str,
) -> list[dict]:
    """Search the local TVSeries/Movie library via FTS5.

    Returns candidates in the same dict shape as LLM search results so the
    frontend can reuse the same selection UI.
    """
    results: list[dict] = []
    norm = normalize_title(search_title)

    if content_type != "movie":
        # Search TV series
        candidate_ids = await fts_service.search_series_fts(db, search_title, limit=20)
        if candidate_ids:
            from sqlalchemy import select as sa_select
            res = await db.execute(sa_select(TVSeries).where(TVSeries.id.in_(candidate_ids)))
            for s in res.scalars().all():
                titles = [s.title_cn, s.title_en, *(s.aliases or [])]
                score = max((similarity_score(norm, t) for t in titles if t), default=0)
                if score < FUZZY_THRESHOLD:
                    continue
                results.append({
                    "content_type": "tv",
                    "title_cn": s.title_cn,
                    "title_en": s.title_en,
                    "original_title": s.original_title,
                    "external_id": s.external_id,
                    "external_source": s.external_source or "local_match",
                    "description": s.description,
                    "poster_url": s.poster_url,
                    "rating": s.rating,
                    "genre": s.genre,
                    "status": s.status,
                    "content_type_detail": "tv",
                    "_local_id": s.id,
                    "_score": score,
                })

    if content_type != "tv":
        # Search movies
        candidate_ids = await fts_service.search_movie_fts(db, search_title, limit=20)
        if candidate_ids:
            from sqlalchemy import select as sa_select
            res = await db.execute(sa_select(Movie).where(Movie.id.in_(candidate_ids)))
            for m in res.scalars().all():
                titles = [m.title_cn, m.title_en, *(m.aliases or [])]
                score = max((similarity_score(norm, t) for t in titles if t), default=0)
                if score < FUZZY_THRESHOLD:
                    continue
                results.append({
                    "content_type": "movie",
                    "title_cn": m.title_cn,
                    "title_en": m.title_en,
                    "original_title": m.original_title,
                    "external_id": m.external_id,
                    "external_source": m.external_source or "local_match",
                    "description": m.description,
                    "poster_url": m.poster_url,
                    "rating": m.rating,
                    "genre": m.genre,
                    "status": m.status,
                    "content_type_detail": "movie",
                    "_local_id": m.id,
                    "_score": score,
                })

    # Sort by score descending
    results.sort(key=lambda r: r.get("_score", 0), reverse=True)
    return results


async def manual_link_metadata(
    db: AsyncSession,
    resource: Any,
    channel: Any,
    selected_result: dict,
) -> TVSeries | Movie:
    """Manually link a resource to user-selected metadata.

    Creates/updates the entity, sets resource FKs, upserts the
    ChannelRawTitleMapping so future identical titles auto-link.
    """
    if selected_result.get("content_type") == "movie":
        entity = await create_or_update_movie_from_external(db, selected_result)
        resource.movie_id = entity.id
        resource.series_id = None
        series_id = None
        movie_id = entity.id
        content_type = "movie"
    else:
        entity = await create_or_update_series_from_external(db, selected_result)
        resource.series_id = entity.id
        resource.movie_id = None
        series_id = entity.id
        movie_id = None
        content_type = "tv"

    resource.metadata_matched_at = utcnow()

    # Upsert ChannelRawTitleMapping
    # Use search_title_key so future resources from the same work (different
    # episode/resolution) also auto-link.
    search_key = normalize_title(extract_search_title(resource))
    if search_key:
        existing = await db.execute(
            select(ChannelRawTitleMapping).where(
                ChannelRawTitleMapping.channel_id == channel.id,
                ChannelRawTitleMapping.search_title_key == search_key,
            )
        )
        mapping = existing.scalars().first()
        if mapping:
            mapping.series_id = series_id
            mapping.movie_id = movie_id
            mapping.content_type = content_type
        else:
            mapping = ChannelRawTitleMapping(
                channel_id=channel.id,
                raw_title=resource.title_raw,
                search_title_key=search_key,
                content_type=content_type,
                series_id=series_id,
                movie_id=movie_id,
            )
            db.add(mapping)
    else:
        # Fallback: use raw_title as key when extraction yields nothing
        existing = await db.execute(
            select(ChannelRawTitleMapping).where(
                ChannelRawTitleMapping.channel_id == channel.id,
                ChannelRawTitleMapping.raw_title == resource.title_raw,
            )
        )
        mapping = existing.scalars().first()
        if mapping:
            mapping.series_id = series_id
            mapping.movie_id = movie_id
            mapping.content_type = content_type
        else:
            mapping = ChannelRawTitleMapping(
                channel_id=channel.id,
                raw_title=resource.title_raw,
                search_title_key=resource.title_raw,
                content_type=content_type,
                series_id=series_id,
                movie_id=movie_id,
            )
            db.add(mapping)

    await db.flush()
    return entity
